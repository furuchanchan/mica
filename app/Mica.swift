import AppKit
import SwiftUI
import Combine

private let mint = Color(red: 0.74, green: 0.85, blue: 1.0)
#if QA
private let servicePort = 18771
#else
// 入っている Mica と別にもう1つ動かす時だけ、環境変数 MICA_APP_PORT で替える（service/mica_config.py と同じ）
private let servicePort = Int(ProcessInfo.processInfo.environment["MICA_APP_PORT"] ?? "") ?? 18770
#endif
// 字の影は付けない：字がにじんで二重に見える（2026-11-19）。QA 版だけ、直す前を撮り比べるために戻せる
#if QA
private let qaOldShadow = ProcessInfo.processInfo.environment["MICA_QA_SHADOW"] != nil
#else
private let qaOldShadow = false
#endif
private let serviceURL = "http://127.0.0.1:\(servicePort)"

struct Segment: Decodable, Identifiable {
    let id: String
    let start: Double
    let end: Double?
    let who: String
    let text: String
    let speaker_status: String?
    let verification_note: String?
    let ai_text: String?
}
struct TranscriptNote: Decodable, Identifiable, Equatable {
    let id: String
    let start: Double
    let text: String
}
struct TranscriptTurn: Decodable, Identifiable, Equatable {
    let id: String
    let start: Double
    let who: String
    let draft: Bool
    let text: String
    let notes: [TranscriptNote]
}
struct Advice: Decodable, Identifiable {
    let text: String; let updated_at: Double?; var question: String?
    var request_id: String?; var mode: String?; var source_notice: String?; var copy_text: String?; var read_through: Double?
    var id: String { request_id ?? String(updated_at ?? 0) }
}
struct Brief: Decodable {
    var purpose: String = ""; var ends_at: Double?; var automatic: Bool = false; var todos: [String]?
    var context: MeetingContext?; var updated_at: Double?; var auto_preparation: Preparation?; var context_mode: String?; var recognition_terms: [String]?
}
struct Preparation: Decodable { var status: String?; var message: String? }
struct AIStatus: Decodable { var phase: String?; var error: String? }
struct Analysis: Decodable { var phase: String?; var message: String?; var text: String?; var error: String? }
struct Memory: Decodable { var summary: String?; var decisions: [String]?; var open_questions: [String]?; var actions: [String]?; var pending_segments: Int? }
struct SessionItem: Decodable, Identifiable { var id: String; var title: String; var phase: String }
struct SessionList: Decodable { var sessions: [SessionItem] }
struct Accepted: Decodable { var phase: String }
struct Recording: Decodable { var session_id: String; var mode: String?; var title: String? }
struct RecordAccepted: Decodable { var accepted: Bool }

struct Meeting: Decodable {
    var title: String?; var status: String?; var segments: [Segment] = []
    var transcript_turns: [TranscriptTurn]?
    var recovery_message: String?; var error: String?; var capture_warning: String?; var session_id: String?; var advice: Advice?
    var capture_active: Bool?; var capture_elapsed_seconds: Double?
    var auto_advice: Advice?; var manual_advice: Advice?; var auto_ai_status: AIStatus?
    var ai_brief: Brief?
    var brief: Brief?; var ai_status: AIStatus?; var memory: Memory?; var analysis: Analysis?
    var advice_history: [Advice]?; var active_session_id: String?
    var recording: Recording?; var capabilities: Capabilities?
}
struct MeetingContext: Decodable {
    var source_id: String?; var title: String?; var updated_at: String?
    var text: String = ""; var error: String?
    init() {}
    enum CodingKeys: String, CodingKey { case source_id, title, updated_at, text, error }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        source_id = try values.decodeIfPresent(String.self, forKey: .source_id)
        title = try values.decodeIfPresent(String.self, forKey: .title)
        updated_at = try values.decodeIfPresent(String.self, forKey: .updated_at)
        text = try values.decodeIfPresent(String.self, forKey: .text) ?? ""
        error = try values.decodeIfPresent(String.self, forKey: .error)
    }
}
struct Source: Decodable, Identifiable { let id: String; let title: String; let updated_at: String }
struct Sources: Decodable { let sources: [Source] }
struct Install: Decodable { var status: String?; var message: String? }
// つないである機能。advice＝OpenAI の鍵（会議中の助言と質問）、analysis＝設定 analysis.engine（会議後の分析）
struct Capabilities: Decodable { var advice: Bool?; var analysis: Bool? }
struct APIError: Decodable { let error: String }

@MainActor final class MeetingModel: ObservableObject {
    @Published var meeting = Meeting()
    @Published var context = MeetingContext()
    @Published var sources: [Source] = []
    @Published var install = Install()
    @Published var purpose = "" { didSet { scheduleSave() } }
    @Published var question = ""
    @Published var minutes = "" { didSet {
        if !restoring {
            endsAt = Double(minutes).flatMap { $0 > 0 && $0 <= 1440 ? Date().timeIntervalSince1970 + $0 * 60 : nil }
            scheduleSave()
        }
    } }
    @Published var answer = ""
    @Published var isThinking = false
    @Published var isSubmitting = false
    var isAutoThinking: Bool { meeting.auto_ai_status?.phase == "running" }
    @Published var automatic = true { didSet { scheduleSave() } }
    @Published var sessions: [SessionItem] = []
    @Published var selectedSession: String? = nil
    @Published var panel = "live"
    @Published var saving = false
    @Published var compact = false
    @Published var pinned = false
    @Published var error: String?
    @Published var questionError: String?
    @Published var toast: String?
    @Published var connected = false
    @Published var showSources = false
    @Published var showSettings = false
    @Published var recordPending: String?
    @Published var confirmStop = false
    private var recordToken = UUID()
    @Published var now = Date()
    private var timer: Task<Void, Never>?
    private var service: Process?
    private var restoring = false
    private var saveTask: Task<Void, Never>?
    private var endsAt: Double?
    private var draftActiveID: String?
    private var hasDraftActiveID = false
    var remainingSeconds: Double? {
        guard let end = endsAt else { return nil }
        return max(0, end - now.timeIntervalSince1970)
    }

    var remainingLabel: String {
        if meeting.status == "録画終了" { return "会議は終了しました" }
        guard let seconds = remainingSeconds else { return "未設定" }
        let count = Int(ceil(seconds))
        return count > 0 ? "残り\(count / 60)分\(count % 60)秒" : "予定時刻を過ぎています"
    }
    private var answerStamp: Double?
    private var adviceTask: Task<Void, Never>?

    init() { timer = Task { await run() } }
    deinit { timer?.cancel(); adviceTask?.cancel() }

    func request<T: Decodable>(_ path: String, body: [String: Any]? = nil) async throws -> T {
        var req = URLRequest(url: URL(string: serviceURL + path)!)
        req.timeoutInterval = body == nil ? 5 : 40
        req.setValue(serviceURL, forHTTPHeaderField: "Origin")
        if let body {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse, (200...202).contains(http.statusCode) else {
            let message = (try? JSONDecoder().decode(APIError.self, from: data))?.error ?? "処理できませんでした。少し待ってから試してください。"
            throw NSError(domain: "Mica", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
    private func startService() {
        #if !QA
        guard service == nil, let resources = Bundle.main.resourceURL else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [resources.appendingPathComponent("mica_service.py").path]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run(); service = process }
        catch { self.error = "文字起こしサービスを起動できませんでした。" }
        #endif
    }
    func stopService() { if let service, service.isRunning { service.terminate() } }
    var statePath: String {
        guard let selectedSession else { return "/api/state" }
        var parts = URLComponents(string: serviceURL + "/api/state")!
        parts.queryItems = [URLQueryItem(name: "session_id", value: selectedSession)]
        return parts.url!.absoluteString.replacingOccurrences(of: serviceURL, with: "")
    }
    func chooseSession(_ id: String?) {
        selectedSession = id; panel = "live"; error = nil; questionError = nil
        meeting = Meeting(); restore(nil); answer = ""; answerStamp = nil
        hasDraftActiveID = false
    }
    private func restore(_ brief: Brief?) {
        restoring = true
        purpose = brief?.purpose ?? ""
        endsAt = brief?.ends_at
        minutes = endsAt.map { String(max(0, Int(ceil(($0 - Date().timeIntervalSince1970) / 60)))) } ?? ""
        automatic = brief?.automatic ?? true
        context = brief?.context ?? MeetingContext()
        restoring = false
    }
    func scheduleSave() {
        guard !restoring, let session = meeting.session_id else { return }
        saveTask?.cancel(); saving = true
        let body: [String: Any] = ["session_id": session, "purpose": purpose, "automatic": automatic, "ends_at": endsAt as Any? ?? NSNull(), "active_session_id": draftActiveID as Any? ?? NSNull()]
        saveTask = Task {
            try? await Task.sleep(for: .milliseconds(450))
            guard !Task.isCancelled else { return }
            do { let _: Brief = try await request("/api/brief", body: body); saving = false }
            catch { saving = false; self.error = "目的の保存に失敗しました。\(error.localizedDescription)" }
        }
    }
    private func run() async {
        do { let _: Meeting = try await request("/api/state") }
        catch { startService(); try? await Task.sleep(for: .seconds(1)) }
        var tick = 0
        while !Task.isCancelled {
            now = Date()
            do {
                let requestedSelection = selectedSession
                let next: Meeting = try await request(statePath)
                guard requestedSelection == selectedSession else { continue }
                if next.session_id == "__next__" {
                    if hasDraftActiveID && next.active_session_id != draftActiveID {
                        selectedSession = nil; hasDraftActiveID = false; continue
                    }
                    draftActiveID = next.active_session_id; hasDraftActiveID = true
                }
                if next.session_id != meeting.session_id {
                    adviceTask?.cancel(); isThinking = false
                    restore(next.brief); question = ""; answer = ""
                    error = nil; questionError = nil; answerStamp = nil
                    panel = next.analysis?.phase == "complete" ? "after" : "live"
                } else if !saving, let brief = next.brief,
                          brief.purpose != purpose || brief.ends_at != endsAt || brief.automatic != automatic {
                    restore(brief)
                }
                meeting = next; connected = true
                if (recordPending == "start" && next.recording != nil) || (recordPending == "stop" && next.recording == nil) {
                    recordPending = nil
                }
                isThinking = isSubmitting || next.ai_status?.phase == "running"
                if let advice = next.advice, advice.updated_at != answerStamp {
                    answer = advice.text; answerStamp = advice.updated_at
                }
                if let background = next.brief?.context { context = background }
                if tick % 5 == 0 {
                    install = try await request("/api/install")
                    let list: SessionList = try await request("/api/sessions"); sessions = list.sessions
                }
            } catch {
                connected = false
                // 会話の無い短い録音は分析側が消す。消えた会議を表示し続けて［録音する］まで押せなくならないよう、いまの会議に戻す
                if let selected = selectedSession, selected != "__next__", error.localizedDescription.contains("会議フォルダが見つかりません") {
                    chooseSession(nil)
                }
            }
            tick += 1
            try? await Task.sleep(for: .seconds(2))
        }
    }
    /// ［録音］［止める］。見張り役（shodan_watch.py）が受け取って録画を始める・止める。
    func setRecording(_ action: String) {
        guard recordPending == nil else { return }
        let token = UUID(); recordToken = token
        recordPending = action; error = nil
        if action == "start" { chooseSession(nil) }
        Task {
            do { let _: RecordAccepted = try await request("/api/recording", body: ["action": action]) }
            catch { if recordToken == token { recordPending = nil; self.error = error.localizedDescription }; return }
            try? await Task.sleep(for: .seconds(30))
            if recordToken == token && recordPending == action {
                recordPending = nil
                self.error = action == "start" ? "録音を始められませんでした。少し待ってから、もう一度押してください。" : "録音を止められませんでした。もう一度押してください。"
            }
        }
    }
    func loadSources() {
        showSources = true
        Task {
            do { let result: Sources = try await request("/api/context/sources"); sources = result.sources }
            catch { self.error = "Claude Codeのプロジェクト一覧を取得できません。" }
        }
    }
    func selectSource(_ source: Source?, automatic: Bool = false) {
        Task {
            do {
                let next: MeetingContext = try await request("/api/context", body: ["mode": automatic ? "auto" : (source == nil ? "off" : "manual"), "source_id": source?.id as Any? ?? NSNull(), "session_id": meeting.session_id as Any? ?? NSNull(), "active_session_id": draftActiveID as Any? ?? NSNull()])
                context = next; showSources = false
            } catch { self.error = error.localizedDescription }
        }
    }
    func ask(_ prompt: String? = nil) {
        guard !isThinking, !isSubmitting, let session = meeting.session_id, !meeting.segments.isEmpty else { return }
        let asking = prompt ?? (question.isEmpty ? "目的に向けて、次に何を確認すべき？" : question)
        if let prompt { question = prompt }
        isThinking = true; isSubmitting = true; questionError = nil; panel = "live"
        let body: [String: Any] = ["session_id":session,"purpose":purpose,"question":asking]
        adviceTask = Task {
            defer { isSubmitting = false }
            do {
                let _: Brief = try await request("/api/brief", body: ["session_id":session,"purpose":purpose,"automatic":automatic,"ends_at":endsAt as Any? ?? NSNull()])
                let _: Accepted = try await request("/api/advice", body: body)
            } catch {
                if !Task.isCancelled && meeting.session_id == session { questionError = error.localizedDescription; isThinking = false }
            }
        }
    }
    func copyTranscript(recent: Bool = false, prompt: Bool = false) {
        if prompt && meeting.ai_brief == nil { toast = "AI用の確認済み情報を取得中です"; return }
        let cutoff = (meeting.segments.last?.start ?? 0) - 180
        let lines = meeting.segments.filter { !recent || $0.start >= cutoff }.map {
            "[\(clock($0.start))] \($0.who): \(prompt ? ($0.ai_text ?? "［聞き取り未確認］") : $0.text)" + (prompt ? "" : ($0.verification_note.map { "\n※要確認: " + $0 } ?? ""))
        }.joined(separator: "\n")
        var text = lines
        if prompt {
            let purpose = meeting.ai_brief?.purpose ?? ""
            let context = meeting.ai_brief?.context ?? MeetingContext()
            text = "会議の目的: \(purpose.isEmpty ? "未設定" : purpose)\n残り時間: \(remainingLabel)\n\n背景（参照資料）:\n\(context.text.isEmpty ? "未接続" : context.text)\n\n文字起こし（誤認識の可能性あり）:\n\(lines)\n\n相談: \(question.isEmpty ? "次の一手・確認質問・時間配分を短く教えてください。" : question)"
        }
        copy(text, message: prompt ? "AIに渡す内容をコピーしました" : "文字起こしをコピーしました")
    }
    func copy(_ text: String, message: String) {
        NSPasteboard.general.clearContents()
        toast = NSPasteboard.general.setString(text, forType: .string) ? message : "コピーできませんでした"
        Task { try? await Task.sleep(for: .seconds(2)); toast = nil }
    }
    func toggleCompact() {
        compact.toggle()
        guard let window = NSApp.windows.first(where: { $0.identifier?.rawValue == "mica" }) else { return }
        window.minSize = compact ? NSSize(width: 430, height: 440) : NSSize(width: 500, height: 500)
        window.setContentSize(compact ? NSSize(width: 460, height: 570) : NSSize(width: 640, height: 690))
    }
    func togglePin() {
        pinned.toggle()
        NSApp.windows.first(where: { $0.identifier?.rawValue == "mica" })?.level = pinned ? .floating : .normal
    }
}
func clock(_ seconds: Double) -> String { let n = max(0, Int(seconds)); return String(format: "%02d:%02d", n / 60, n % 60) }

private struct TranscriptParagraph: View {
    let turn: TranscriptTurn
    @State private var showingNotes = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(turn.who)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(turn.who == "自分" ? mint : .white.opacity(0.76))
                Text(clock(turn.start)).font(.system(size: 10, design: .monospaced)).foregroundStyle(.white.opacity(0.55))
                if turn.draft {
                    Text("速報").font(.system(size: 10)).foregroundStyle(.white.opacity(0.65))
                }
                if !turn.notes.isEmpty {
                    Button { showingNotes.toggle() } label: {
                        Image(systemName: "exclamationmark.circle")
                            .font(.system(size: 12))
                            .foregroundStyle(.orange)
                            .padding(.horizontal, 3)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("聞き取りを確認")
                    .help("聞き取りを確認")
                    .popover(isPresented: $showingNotes, arrowEdge: .bottom) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("聞き取りを確認").font(.system(size: 12, weight: .semibold))
                            ScrollView {
                                VStack(alignment: .leading, spacing: 8) {
                                    ForEach(turn.notes) { note in
                                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                                            Text(clock(note.start)).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                                            Text(note.text).font(.system(size: 12)).textSelection(.enabled)
                                        }
                                    }
                                }.frame(maxWidth: .infinity, alignment: .leading)
                            }.frame(maxHeight: 180)
                        }.padding(12).frame(width: 310)
                    }
                }
            }
            Text(turn.text)
                .font(.system(size: 14))
                .lineSpacing(3)
                .foregroundStyle(.white.opacity(turn.draft ? 0.82 : 0.96))
                .textSelection(.enabled)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct Backdrop: NSViewRepresentable {
    var strength: Double
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView(); view.material = .hudWindow; view.blendingMode = .behindWindow; view.state = .active; view.alphaValue = strength; return view
    }
    func updateNSView(_ view: NSVisualEffectView, context: Context) { view.alphaValue = strength }
}
// One reading surface; meeting setup stays behind a single control.
struct QuietButton: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(.white.opacity(configuration.isPressed ? 0.16 : 0.07), in: Capsule())
            .contentShape(Capsule())
    }
}

struct MicaView: View {
    @ObservedObject var model: MeetingModel
    @State private var followTranscript = true
    @State private var showAppearance = false
    @AppStorage("glassTransparency") private var glassTransparency = 0.50
    private var transparency: Double { min(0.85, max(0, glassTransparency)) }
    var adviceConnected: Bool { model.meeting.capabilities?.advice != false }
    var canAsk: Bool { model.connected && adviceConnected && !model.meeting.segments.isEmpty && !model.isThinking }
    var isRecording: Bool { model.connected && model.meeting.capture_active == true }
    var meetingStatus: String {
        guard model.connected else { return "接続中" }
        if isRecording {
            let label = ["schedule", "manual"].contains(model.meeting.recording?.mode ?? "") && model.meeting.recording?.session_id == model.meeting.session_id ? "対面で録音中" : "録音中"
            return model.meeting.recovery_message != nil || model.meeting.status == "復旧待ち" ? label + "・復旧待ち" : label
        }
        let status = model.meeting.status ?? "待機中"
        return ["録音中", "recording"].contains(status) ? "録音状態未確認" : status
    }
    var title: String { model.meeting.session_id == "__next__" ? "次の会議" : model.meeting.title ?? "会議を待っています" }
    var inset: CGFloat { model.compact ? 20 : 26 }

    var body: some View {
        VStack(spacing: 0) {
            topBar
            meetingBar
            navigation
            Rectangle().fill(.white.opacity(0.11)).frame(height: 1)
            Group {
                switch model.panel {
                case "transcript": transcript
                case "after": afterMeeting
                case "history": adviceHistory
                default: assistant
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            if let error = model.error ?? model.meeting.error ?? model.meeting.capture_warning ?? model.meeting.recovery_message {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.system(size: 12)).foregroundStyle(.orange)
                    .textSelection(.enabled).padding(.horizontal, inset).padding(.bottom, 8)
            }
            composer
        }
        .foregroundStyle(.white.opacity(0.96))
        .shadow(color: .black.opacity(qaOldShadow ? 0.65 : 0), radius: qaOldShadow ? 1.5 : 0, x: 0, y: qaOldShadow ? 1 : 0)
        .background {
            ZStack {
                Backdrop(strength: 1 - transparency)
                Color.black.opacity(0.44 * (1 - transparency))
            }
        }
        .ignoresSafeArea(.container, edges: .top)
        .preferredColorScheme(.dark)
        .sheet(isPresented: $model.showSources) { SourceSheet(model: model) }
    }

    var topBar: some View {
        HStack(spacing: 9) {
            Text("mica").font(.system(size: 17, weight: .semibold, design: .rounded)).tracking(-0.7)
            Spacer(minLength: 8)
            HStack(spacing: 5) {
                Circle().fill(!model.connected ? .orange : isRecording ? Color.green.opacity(0.85) : .white.opacity(0.55)).frame(width: 5, height: 5)
                Text(meetingStatus).font(.system(size: 11)).foregroundStyle(.white.opacity(0.72))
            }
            if let end = model.meeting.capture_elapsed_seconds ?? model.meeting.segments.last?.end {
                Text(clock(end)).font(.system(size: 11, design: .monospaced)).foregroundStyle(.white.opacity(0.64)).padding(.leading, 2)
            }
            recordButton
            Button { showAppearance.toggle() } label: {
                Image(systemName: "circle.lefthalf.filled").frame(width: 30, height: 30).contentShape(Rectangle())
            }.help("背景の透明度").accessibilityLabel("背景の透明度")
                .popover(isPresented: $showAppearance, arrowEdge: .bottom) {
                    VStack(alignment: .leading, spacing: 14) {
                        HStack {
                            Text("背景の透明度").font(.system(size: 13, weight: .semibold))
                            Spacer()
                            Text("\(Int((transparency * 100).rounded()))%")
                                .font(.system(size: 12, design: .monospaced)).foregroundStyle(.secondary)
                        }
                        Slider(value: $glassTransparency, in: 0...0.85, step: 0.05)
                            .accessibilityLabel("背景の透明度")
                            .accessibilityValue("\(Int((transparency * 100).rounded()))パーセント")
                        HStack {
                            Text("くっきり"); Spacer(); Text("薄く・クリアに")
                        }.font(.system(size: 11)).foregroundStyle(.secondary)
                        Text("文字の濃さは変えず、背景だけを調整します。")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                        HStack {
                            Button("標準に戻す") { glassTransparency = 0.50 }
                            Spacer()
                            Button("完了") { showAppearance = false }.keyboardShortcut(.defaultAction)
                        }.font(.system(size: 12))
                    }.padding(18).frame(width: 280).preferredColorScheme(.dark)
                }
            Button { model.togglePin() } label: { Image(systemName: model.pinned ? "pin.fill" : "pin").frame(width: 30, height: 30).contentShape(Rectangle()) }
                .foregroundStyle(model.pinned ? mint : .white.opacity(0.72)).help("最前面に固定").accessibilityLabel("最前面に固定")
            Button { model.toggleCompact() } label: { Image(systemName: model.compact ? "arrow.up.left.and.arrow.down.right" : "arrow.down.right.and.arrow.up.left").frame(width: 30, height: 30).contentShape(Rectangle()) }
                .help(model.compact ? "標準サイズに戻す" : "コンパクト表示").accessibilityLabel("コンパクト表示")
        }.buttonStyle(.plain).padding(.leading, 78).padding(.trailing, 15).frame(height: 44)
    }

    var recordButton: some View {
        let recording = model.meeting.recording != nil
        let pending = model.recordPending
        return Button { if recording { model.confirmStop = true } else { model.setRecording("start") } } label: {
            HStack(spacing: 5) {
                if pending != nil { ProgressView().controlSize(.mini) }
                else { Image(systemName: recording ? "stop.fill" : "circle.fill").font(.system(size: 7)).foregroundStyle(recording ? Color.white : Color.red) }
                Text(pending == "start" ? "開始中" : pending == "stop" ? "停止中" : recording ? "止める" : "録音する")
                    .font(.system(size: 11, weight: .medium))
            }.padding(.horizontal, 10).frame(height: 24).background(.white.opacity(0.1), in: Capsule()).contentShape(Capsule())
        }
        .disabled(!model.connected || pending != nil)
        .help(recording ? "録音を止めて、会議後の分析を始めます" : "対面の会議などを、いまから録音します")
        .accessibilityLabel(recording ? "録音を止める" : "録音を始める")
        .confirmationDialog("録音を止めますか？", isPresented: $model.confirmStop) {
            Button("止める", role: .destructive) { model.setRecording("stop") }
            Button("続ける", role: .cancel) {}
        } message: { Text("止めると、会議後の分析を始めます。") }
    }

    var meetingBar: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 12) {
                Menu {
                    Button("進行中・直近の会議") { model.chooseSession(nil) }
                    Button("次の会議（自動準備）") { model.chooseSession("__next__"); model.showSettings = true }
                    Divider()
                    ForEach(model.sessions) { session in
                        Button(session.title + " · " + String(session.id.prefix(15))) { model.chooseSession(session.id) }
                    }
                } label: {
                    HStack(spacing: 6) {
                        Text(title).font(.system(size: 14, weight: .medium)).lineLimit(1)
                        Image(systemName: "chevron.down").font(.system(size: 9, weight: .medium)).foregroundStyle(.white.opacity(0.6))
                    }
                }.menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize(horizontal: false, vertical: true).accessibilityLabel("会議を切り替える")
                Spacer(minLength: 0)
                Button { model.showSettings.toggle() } label: {
                    Label("目的・背景", systemImage: "slider.horizontal.3").font(.system(size: 11)).fixedSize()
                }.buttonStyle(.plain).foregroundStyle(.white.opacity(0.72)).accessibilityLabel("目的・背景")
                    .popover(isPresented: $model.showSettings, arrowEdge: .bottom) { MeetingSetup(model: model) }
            }
            if !model.purpose.isEmpty {
                HStack(spacing: 6) {
                    Image(systemName: "scope").font(.system(size: 10))
                    Text(model.purpose).font(.system(size: 11)).lineLimit(1)
                    Spacer(minLength: 0)
                    if model.remainingSeconds != nil && model.meeting.status != "録画終了" {
                        Text(model.remainingLabel).font(.system(size: 10, design: .monospaced)).fixedSize()
                    }
                }.foregroundStyle(.white.opacity(0.65))
            }
            if let todos = model.meeting.brief?.todos, !todos.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(todos.prefix(5).enumerated()), id: \.offset) { _, todo in
                        Text("・" + todo).font(.system(size: 11)).lineLimit(1)
                    }
                }.foregroundStyle(.white.opacity(0.65)).accessibilityLabel("やること")
            }
        }.padding(.horizontal, inset).padding(.top, 8).padding(.bottom, 12)
    }

    var navigation: some View {
        HStack(spacing: 5) {
            tab("アシスト", id: "live")
            tab("文字起こし", id: "transcript")
            tab("会議後", id: "after")
            Spacer(minLength: 0)
            Button { model.panel = model.panel == "history" ? "live" : "history" } label: {
                Image(systemName: "clock.arrow.circlepath").frame(width: 30, height: 30).contentShape(Rectangle())
            }.buttonStyle(.plain).foregroundStyle(.white.opacity(model.panel == "history" ? 1 : 0.62)).help("相談履歴").accessibilityLabel("相談履歴")
            Menu {
                if model.panel == "after", let text = model.meeting.analysis?.text { Button("分析をコピー") { model.copy(text, message: "分析をコピーしました") } }
                if !model.answer.isEmpty { Button("AIの回答をコピー") { model.copy(model.meeting.advice?.copy_text ?? model.answer, message: (model.meeting.advice?.source_notice ?? "").isEmpty ? "回答をコピーしました" : "参照元の訂正通知をコピーしました") } }
                Button("文字起こしの全文をコピー") { model.copyTranscript() }
                Button("直近3分をコピー") { model.copyTranscript(recent: true) }
                Divider()
                Button("AIに渡す内容をコピー") { model.copyTranscript(prompt: true) }.keyboardShortcut("c", modifiers: [.command, .shift])
            } label: { Image(systemName: "doc.on.doc").frame(width: 28, height: 30) }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().disabled(model.meeting.segments.isEmpty).help("コピー").accessibilityLabel("コピー")
        }.padding(.horizontal, inset - 5).padding(.bottom, 10)
    }
    func tab(_ text: String, id: String) -> some View {
        Button { model.panel = id } label: {
            Text(text).font(.system(size: 12, weight: model.panel == id ? .semibold : .regular))
                .padding(.horizontal, 12).frame(height: 30)
                .foregroundStyle(.white.opacity(model.panel == id ? 1 : 0.65))
                .background(.white.opacity(model.panel == id ? 0.13 : 0), in: RoundedRectangle(cornerRadius: 8))
                .contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityAddTraits(model.panel == id ? .isSelected : [])
    }

    func adviceCard(_ advice: Advice, title: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 7) {
                Image(systemName: "sparkle").foregroundStyle(mint)
                Text(title).font(.system(size: 11, weight: .medium)).foregroundStyle(.white.opacity(0.64))
                Spacer()
                if let read = advice.read_through { Text("会話 " + clock(read) + " 時点").font(.system(size: 10)).foregroundStyle(.white.opacity(0.55)) }
            }
            if let notice = advice.source_notice, !notice.isEmpty {
                Label(notice, systemImage: "info.circle").font(.system(size: 11)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            ReportText(text: advice.text)
        }
    }

    var assistant: some View {
        let manualError = model.questionError ?? (model.isThinking ? nil : model.meeting.ai_status?.error)
        let autoError = model.meeting.auto_ai_status?.error
        let ended = model.meeting.status == "録画終了"
        return VStack(spacing: 0) {
            if manualError != nil || autoError != nil {
                VStack(alignment: .leading, spacing: 4) {
                    if let manualError { Text("今回の質問: " + manualError) }
                    if let autoError { Text("自動ヒント: " + autoError) }
                }
                .font(.system(size: 11)).foregroundStyle(.orange)
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, inset).padding(.top, 8)
            }
            if model.answer.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "waveform").font(.system(size: 28, weight: .light)).foregroundStyle(mint.opacity(0.85))
                    Text(!adviceConnected ? "助言は、OpenAI の鍵をつなぐと使えます" : model.isThinking ? "質問への回答を考えています" : model.isAutoThinking ? "自動ヒントを考えています" : model.meeting.segments.isEmpty ? "会話を待っています" : ended ? "この会議について聞く" : "いま、何を確認する？")
                        .font(.system(size: 19, weight: .medium))
                    Text(!adviceConnected ? "録音と文字起こしは、鍵なしでこのまま使えます。つなぎ方は README の「つなぐと増える機能」にあります。" : !model.meeting.segments.isEmpty ? (ended ? "会議は終わっています。記録をもとに、下から質問できます。" : "自動ヒントの作成中も、下から質問できます。") : model.meeting.recording == nil ? "対面の会議は、予定の時刻か右上の［録音する］で始まります。" : "会議が始まると、ここに提案が届きます。")
                        .font(.system(size: 12)).foregroundStyle(.white.opacity(0.65))
                    if model.isThinking || model.isAutoThinking { ProgressView().controlSize(.small) }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        if model.isThinking {
                            HStack { ProgressView().controlSize(.small); Text("質問への回答を作成中").font(.system(size: 11)).foregroundStyle(.white.opacity(0.65)) }
                        }
                        if let advice = model.meeting.advice {
                            adviceCard(advice, title: advice.mode == "manual" ? "質問への回答" : "自動ヒント")
                        }
                        if let manual = model.meeting.manual_advice, let automatic = model.meeting.auto_advice, manual.id != automatic.id {
                            Divider()
                            adviceCard(automatic, title: "自動ヒント")
                        }
                        if model.isAutoThinking {
                            HStack { ProgressView().controlSize(.small); Text("自動ヒントを作成中・質問できます").font(.system(size: 11)).foregroundStyle(.white.opacity(0.65)) }
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(inset)
                }
            }
        }
    }

    var transcript: some View {
        let turns = model.meeting.transcript_turns ?? []
        return VStack(alignment: .leading, spacing: 0) {
            HStack {
                Spacer()
                Toggle("最新に追従", isOn: $followTranscript).toggleStyle(.checkbox).font(.system(size: 10)).foregroundStyle(.white.opacity(0.65))
            }.padding(.horizontal, inset).padding(.vertical, 5)
            if turns.isEmpty {
                Text("発言が届くと、ここに表示されます。")
                    .font(.system(size: 13)).foregroundStyle(.white.opacity(0.65)).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 9) {
                            ForEach(turns.suffix(100)) { turn in
                                TranscriptParagraph(turn: turn)
                            }
                            Color.clear.frame(height: 1).id("latest")
                        }.padding(.horizontal, inset).padding(.vertical, 4)
                    }
                    .onAppear { if followTranscript { proxy.scrollTo("latest", anchor: .bottom) } }
                    .onChange(of: turns.last) { _, _ in if followTranscript { proxy.scrollTo("latest", anchor: .bottom) } }
                    .onChange(of: model.meeting.session_id) { _, _ in if followTranscript { proxy.scrollTo("latest", anchor: .bottom) } }
                    .onChange(of: followTranscript) { _, follow in if follow { proxy.scrollTo("latest", anchor: .bottom) } }
                }
            }
        }
    }

    var afterMeeting: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let text = model.meeting.analysis?.text, !text.isEmpty { ReportText(text: text) }
                else {
                    Label("会議後のまとめ", systemImage: "checkmark.bubble").font(.system(size: 15, weight: .medium))
                    Text(model.meeting.analysis?.message ?? (model.meeting.capabilities?.analysis == false ? "会議が終わると、文字起こしを保存します。分析は、Claude をつなぐと使えます（README の「つなぐと増える機能」）。" : "会議が終わると、文字起こしと相談を引き継いで分析します。"))
                        .font(.system(size: 13)).foregroundStyle(.white.opacity(0.72)).lineSpacing(5)
                    if let memory = model.meeting.memory, let summary = memory.summary, !summary.isEmpty {
                        Divider()
                        Text("ここまでの整理 · 暫定").font(.system(size: 11)).foregroundStyle(.white.opacity(0.65))
                        Text(summary).font(.system(size: 14)).lineSpacing(5).textSelection(.enabled)
                        memoryList("決まったこと", memory.decisions)
                        memoryList("未決・確認したいこと", memory.open_questions)
                        memoryList("次にすること", memory.actions)
                    }
                }
                if let error = model.meeting.analysis?.error { Text(error).font(.system(size: 12)).foregroundStyle(.orange).textSelection(.enabled) }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(inset)
        }
    }
    @ViewBuilder func memoryList(_ title: String, _ items: [String]?) -> some View {
        if let items, !items.isEmpty {
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(.white.opacity(0.75))
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in Text("・" + item).font(.system(size: 13)).textSelection(.enabled) }
        }
    }
    var adviceHistory: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                Text("相談履歴").font(.system(size: 15, weight: .medium))
                if (model.meeting.advice_history ?? []).isEmpty { Text("相談した内容が、この会議に残ります。").font(.system(size: 13)).foregroundStyle(.white.opacity(0.65)) }
                ForEach((model.meeting.advice_history ?? []).reversed()) { advice in
                    VStack(alignment: .leading, spacing: 10) {
                        Text(advice.question ?? "自動ヒント").font(.system(size: 13, weight: .semibold)).foregroundStyle(mint)
                        if let notice = advice.source_notice, !notice.isEmpty { Text(notice).font(.system(size: 11)).foregroundStyle(.orange) }
                        ReportText(text: advice.text)
                        if let time = advice.updated_at { Text(Date(timeIntervalSince1970: time), style: .time).font(.system(size: 10)).foregroundStyle(.white.opacity(0.55)) }
                    }
                    Divider()
                }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(inset)
        }
    }

    var composer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.panel != "transcript" {
                HStack(spacing: 7) {
                    Button("次に聞くこと") { model.ask("次に聞くべき質問を教えて") }
                    Button("論点をまとめる") { model.ask("今の論点と未決定事項を整理して") }
                    questionMenu
                    Spacer(minLength: 0)
                }.buttonStyle(QuietButton()).disabled(!canAsk)
            }
            HStack(spacing: 10) {
                TextField(adviceConnected ? "会話について聞く…" : "OpenAI の鍵をつなぐと質問できます", text: $model.question, axis: .vertical)
                    .textFieldStyle(.plain).font(.system(size: 14)).lineLimit(1...3).accessibilityLabel("AIへの質問")
                if model.panel == "transcript" { questionMenu.disabled(!canAsk) }
                if !model.compact { Text("⌘ ↵").font(.system(size: 11)).foregroundStyle(.white.opacity(0.5)) }
                Button { model.ask() } label: {
                    Image(systemName: "arrow.up").font(.system(size: 13, weight: .semibold)).frame(width: 30, height: 30)
                        .background(canAsk ? Color.white.opacity(0.94) : .white.opacity(0.13), in: Circle())
                        .foregroundStyle(canAsk ? Color.black.opacity(0.88) : .white.opacity(0.5))
                }.buttonStyle(.plain).keyboardShortcut(.return, modifiers: .command).disabled(!canAsk).accessibilityLabel("AIに相談")
            }.padding(.leading, 14).padding(.trailing, 8).padding(.vertical, 9)
                .background(.black.opacity(0.15), in: RoundedRectangle(cornerRadius: 14))
                .overlay { RoundedRectangle(cornerRadius: 14).strokeBorder(.white.opacity(0.16)) }
            if let toast = model.toast { Text(toast).font(.system(size: 11)).foregroundStyle(mint).lineLimit(1) }
        }.padding(.horizontal, inset).padding(.top, 12).padding(.bottom, 18)
    }

    var questionMenu: some View {
        Menu {
            if model.panel == "transcript" {
                Button("次に聞くこと") { model.ask("次に聞くべき質問を教えて") }
                Button("論点をまとめる") { model.ask("今の論点と未決定事項を整理して") }
            }
            Button("残り時間の使い方") { model.ask("残り時間をどう使えばいい？") }
        } label: { Image(systemName: "ellipsis").frame(width: 22, height: 24) }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .help("その他の質問").accessibilityLabel("その他の質問")
    }
}

struct MeetingSetup: View {
    @ObservedObject var model: MeetingModel
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                Text("目的・背景").font(.system(size: 16, weight: .semibold))
                Spacer()
                Text(model.saving ? "保存中…" : "自動保存").font(.system(size: 10)).foregroundStyle(.secondary)
                Button { model.showSettings = false } label: { Image(systemName: "xmark").frame(width: 26, height: 26) }.buttonStyle(.plain).accessibilityLabel("設定を閉じる")
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("この会議で決めたいこと").font(.system(size: 12, weight: .medium))
                ZStack(alignment: .topLeading) {
                    if model.purpose.isEmpty { Text("予定と会話から自動で準備します").font(.system(size: 13)).foregroundStyle(.secondary).padding(5).allowsHitTesting(false) }
                    TextEditor(text: $model.purpose).font(.system(size: 13)).scrollContentBackground(.hidden)
                        .frame(height: 70).accessibilityLabel("会議の目的")
                }.padding(8).background(.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 9))
                if let todos = model.meeting.brief?.todos, !todos.isEmpty {
                    Text("やること（会議前に自動で準備）").font(.system(size: 12, weight: .medium))
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(todos.prefix(5).enumerated()), id: \.offset) { _, todo in
                            Text("・" + todo).font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                HStack(spacing: 8) {
                    Text("残り時間").font(.system(size: 12)).foregroundStyle(.secondary)
                    Spacer()
                    TextField("—", text: $model.minutes).textFieldStyle(.roundedBorder).frame(width: 58).multilineTextAlignment(.trailing).accessibilityLabel("残り時間")
                    Text("分").font(.system(size: 12))
                }
            }
            if let preparation = model.meeting.brief?.auto_preparation {
                Text(preparation.message ?? "予定と関連する会話から自動準備")
                    .font(.system(size: 11)).foregroundStyle(preparation.status == "error" ? Color.orange : Color.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Label("会議の背景", systemImage: "link").font(.system(size: 12, weight: .medium))
                    Spacer()
                    Button("調整") { model.showSettings = false; model.loadSources() }.font(.system(size: 12))
                }
                Text(model.context.title ?? (model.meeting.brief?.context_mode == "off" ? "背景を使わない設定です" : "予定と関連するClaudeの会話を自動で探します"))
                    .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2)
                if !model.context.text.isEmpty {
                    DisclosureGroup("取り込んだ背景") { ScrollView { Text(model.context.text).font(.system(size: 12)).frame(maxWidth: .infinity, alignment: .leading).fixedSize(horizontal: false, vertical: true).textSelection(.enabled) }.frame(height: 160) }.font(.system(size: 11))
                }
                if let terms = model.meeting.brief?.recognition_terms, !terms.isEmpty {
                    DisclosureGroup("聞き取りに使う用語") {
                        Text(terms.joined(separator: "、")).font(.system(size: 11)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    }.font(.system(size: 11))
                }
                if let error = model.context.error { Text(error).font(.system(size: 11)).foregroundStyle(.orange) }
            }
            Divider()
            Toggle(isOn: $model.automatic) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("自動ヒント").font(.system(size: 12, weight: .medium))
                    Text("新しい発言から約30秒ごとに提案").font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }.toggleStyle(.switch).controlSize(.small).tint(mint)
        }.padding(22).frame(width: 360).preferredColorScheme(.dark)
    }
}

struct ReportBlock: Identifiable {
    var id: Int
    var text = ""
    var heading = 0
    var quote = false
    var rows: [[String]] = []
}
struct ReportText: View {
    let text: String
    var blocks: [ReportBlock] {
        let lines = text.components(separatedBy: .newlines)
        var result: [ReportBlock] = []
        var index = 0
        while index < lines.count {
            let line = lines[index].trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("|") {
                var rows: [[String]] = []
                while index < lines.count && lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("|") {
                    let row = lines[index].trimmingCharacters(in: .whitespaces)
                    if row.range(of: "^[ |:\\-]+$", options: .regularExpression) == nil {
                        rows.append(row.trimmingCharacters(in: CharacterSet(charactersIn: "|")).components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) })
                    }
                    index += 1
                }
                result.append(ReportBlock(id: result.count, rows: rows))
                continue
            }
            let depth = line.prefix(while: { $0 == "#" }).count
            if depth > 0 && depth <= 6 {
                result.append(ReportBlock(id: result.count, text: String(line.dropFirst(depth)).trimmingCharacters(in: .whitespaces), heading: depth))
            } else if !line.isEmpty {
                result.append(ReportBlock(id: result.count, text: line.hasPrefix(">") ? String(line.dropFirst()).trimmingCharacters(in: .whitespaces) : line, quote: line.hasPrefix(">")))
            }
            index += 1
        }
        return result
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(blocks) { block in
                if !block.rows.isEmpty {
                    ScrollView(.horizontal) {
                        Grid(alignment: .topLeading, horizontalSpacing: 18, verticalSpacing: 12) {
                            ForEach(Array(block.rows.enumerated()), id: \.offset) { rowIndex, cells in
                                GridRow {
                                    ForEach(Array(cells.enumerated()), id: \.offset) { _, cell in
                                        Text(.init(cell)).font(.system(size: 12, weight: rowIndex == 0 ? .semibold : .regular))
                                            .frame(minWidth: 95, maxWidth: 240, alignment: .leading)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                }
                            }
                        }.padding(.vertical, 6)
                    }
                } else {
                    Text(.init(block.text))
                        .font(.system(size: block.heading > 0 ? (block.heading == 1 ? 20 : 16) : 14, weight: block.heading > 0 ? .semibold : .regular))
                        .foregroundStyle(.white.opacity(block.quote ? 0.72 : 0.95)).lineSpacing(6)
                        .padding(.top, block.heading > 0 ? 9 : 0)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }.textSelection(.enabled)
    }
}
struct SourceSheet: View {
    @ObservedObject var model: MeetingModel
    @State private var search = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("会議の背景").font(.system(size: 21, weight: .medium)); Spacer(); Button("閉じる") { model.showSources = false } }
            Text("通常は予定に合う会話を自動で選びます。必要なときだけ、特定のプロジェクトへ変更できます。").font(.system(size: 12)).foregroundStyle(.secondary)
            TextField("プロジェクトを検索", text: $search).textFieldStyle(.roundedBorder)
            List {
                Button { model.selectSource(nil, automatic: true) } label: { Label("会議に合わせて自動で探す", systemImage: "sparkles").padding(.vertical, 4) }.buttonStyle(.plain)
                Button { model.selectSource(nil) } label: { Label("Claudeの背景を使わない", systemImage: "link.badge.plus").padding(.vertical, 4) }.buttonStyle(.plain)
                ForEach(model.sources.filter { search.isEmpty || $0.title.localizedCaseInsensitiveContains(search) }.prefix(100)) { source in
                    Button { model.selectSource(source) } label: {
                        HStack { VStack(alignment: .leading, spacing: 4) { Text(source.title).font(.system(size: 12)); Text(source.updated_at.replacingOccurrences(of: "T", with: " ").prefix(16) + " UTC").font(.system(size: 10)).foregroundStyle(.secondary) }; Spacer(); if model.context.source_id == source.id { Image(systemName: "checkmark").foregroundStyle(mint) } }.padding(.vertical, 5).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                }
            }.listStyle(.inset).clipShape(RoundedRectangle(cornerRadius: 12))
            Text("最新100件を表示。検索で絞り込めます。ChatGPT・Claudeのデスクトップ画面とは接続していません。").font(.system(size: 10)).foregroundStyle(.secondary)
        }.padding(26).frame(width: 420, height: 460).preferredColorScheme(.dark)
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate {
    var window: NSWindow!
    var model: MeetingModel!
    var statusItem: NSStatusItem!
    private var recordItem: NSMenuItem!
    private var pendingMeeting: String?
    #if QA
    private var qaPaper: NSWindow?
    #endif
    func applicationDidFinishLaunching(_ notification: Notification) {
        model = MeetingModel()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 690), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.identifier = NSUserInterfaceItemIdentifier("mica")
        window.title = "Mica — 会議アシスタント"
        window.titleVisibility = .hidden; window.titlebarAppearsTransparent = true
        window.isOpaque = false; window.backgroundColor = .clear
        window.appearance = NSAppearance(named: .darkAqua)
        window.minSize = NSSize(width: 500, height: 500)
        window.isMovableByWindowBackground = true
        window.contentView = NSHostingView(rootView: MicaView(model: model))
        window.delegate = self
        #if QA
        // 検証用：作業中の画面から前面を奪わず、指定の位置（MICA_QA_ORIGIN="x,y"）に出す
        let origin = (ProcessInfo.processInfo.environment["MICA_QA_ORIGIN"] ?? "").split(separator: ",").compactMap { Double($0) }
        if origin.count == 2 {
        window.setFrameOrigin(NSPoint(x: origin[0], y: origin[1]))
        // 後ろに字の詰まった白い紙を置き、透けた字が読めるかを撮って確かめる
        let paper = NSWindow(contentRect: window.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        let darkPaper = ProcessInfo.processInfo.environment["MICA_QA_PAPER"] == "dark"; paper.backgroundColor = darkPaper ? NSColor(white: 0.1, alpha: 1) : .white; paper.level = .floating; paper.ignoresMouseEvents = true
        let label = NSTextField(wrappingLabelWithString: String(repeating: "株式会社サンプル様向け ご提案資料　会議の準備を決めて進める　2026年1月1日　", count: 40))
        label.font = .systemFont(ofSize: 15, weight: .medium); label.textColor = darkPaper ? .clear : .black; label.frame = NSRect(x: 16, y: 16, width: window.frame.width - 32, height: window.frame.height - 32)
        paper.contentView?.addSubview(label); paper.orderFrontRegardless(); qaPaper = paper
        window.level = .floating; window.ignoresMouseEvents = true
        if let panel = ProcessInfo.processInfo.environment["MICA_QA_PANEL"] { Task { try? await Task.sleep(for: .seconds(3)); model.panel = panel } }
        window.orderFrontRegardless()
        } else { window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
        #else
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        #endif
        let mainMenu = NSMenu()
        let appItem = NSMenuItem(); mainMenu.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "Micaを終了", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(title: "編集", action: nil, keyEquivalent: ""); mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "編集"); editItem.submenu = editMenu
        editMenu.addItem(withTitle: "元に戻す", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "切り取り", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "コピー", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "ペースト", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "すべて選択", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        NSApp.mainMenu = mainMenu
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: "Mica")
        let menu = NSMenu(); menu.autoenablesItems = false; menu.delegate = self
        recordItem = NSMenuItem(title: "録音を始める", action: #selector(toggleRecording), keyEquivalent: ""); recordItem.target = self; menu.addItem(recordItem)
        let openItem = NSMenuItem(title: "Micaを開く", action: #selector(showWindow), keyEquivalent: ""); openItem.target = self; menu.addItem(openItem)
        menu.addItem(.separator()); menu.addItem(withTitle: "終了", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"); statusItem.menu = menu
        if let session = pendingMeeting { model.chooseSession(session); pendingMeeting = nil }
    }
    func application(_ application: NSApplication, open urls: [URL]) {
        guard let url = urls.last, url.scheme == "mica", url.host == "meeting",
              let session = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "session" })?.value,
              !session.isEmpty, !session.contains("/"), session != ".", session != ".." else { return }
        if let model { model.chooseSession(session) } else { pendingMeeting = session }
        if window != nil { showWindow() }
    }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    func menuNeedsUpdate(_ menu: NSMenu) {
        let pending = model.recordPending
        recordItem.title = pending == "start" ? "録音を開始中…" : pending == "stop" ? "録音を停止中…" : model.meeting.recording != nil ? "録音を止める…" : "録音を始める"
        recordItem.isEnabled = model.connected && pending == nil
    }
    @objc func toggleRecording() {
        if model.meeting.recording != nil { showWindow(); model.confirmStop = true } else { model.setRecording("start") }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func applicationWillTerminate(_ notification: Notification) { model.stopService() }
}
@main struct MicaApp {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        #if QA
        application.setActivationPolicy(ProcessInfo.processInfo.environment["MICA_QA_ORIGIN"] == nil ? .regular : .accessory)
        #else
        application.setActivationPolicy(.regular)
        #endif
        application.run()
        withExtendedLifetime(delegate) {}
    }
}
