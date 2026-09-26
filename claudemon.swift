// claudemon — a tiny floating dashboard for Claude Code token and agent usage.
// Reads the transcripts Claude Code keeps in ~/.claude/projects. Build: ./build.sh  Run: open Claudemon.app

import AppKit
import Darwin

// MARK: - Data

struct Usage {
    var input = 0, cacheWrite = 0, cacheRead = 0, output = 0
    var total: Int { input + cacheWrite + cacheRead + output }
    static func += (a: inout Usage, b: Usage) {
        a.input += b.input; a.cacheWrite += b.cacheWrite; a.cacheRead += b.cacheRead; a.output += b.output
    }
}

struct Reply {
    let time: Date
    let model: String
    let usage: Usage
    let file: String
}

/// Per-transcript state, so each poll only parses lines appended since the last one.
final class Transcript {
    var offset: UInt64 = 0
    var partial = Data()
    var mtime = Date.distantPast
    var project = "?"
    var lastStop: String?
    var lastContext = 0
    var lastReply: Date?
    var session = ""           // main: its own id; subagent: the main session it belongs to
    var action: String?        // what it is doing now (see noteAction)
    var actionTime: Date?
    // subagents only
    let isAgent: Bool
    var agentType = "agent"
    var agentTask = ""

    init(isAgent: Bool) { self.isAgent = isAgent }
}

enum TimeRange: Int, CaseIterable {
    case hour, fiveHours, day, week

    var label: String { ["1h", "5h", "24h", "7d"][rawValue] }
    var seconds: TimeInterval { [3600.0, 18000.0, 86400.0, 604800.0][rawValue] }
    var buckets: Int { [60, 60, 96, 0][rawValue] }
    var axis: [String] { [["60m", "30m", "now"], ["5h", "2.5h", "now"], ["24h", "12h", "now"], []][rawValue] }
}

/// One subagent's run: when it was active, on which model, and what it cost in tokens.
struct Span {
    let id: String
    let type: String
    let task: String
    let family: String
    let start: Date
    let end: Date
    let tokens: Int
    let running: Bool
    let session: String
    let action: String?
}

/// A live Claude Code session and the subagents it is running right now.
struct NowSession {
    let project, name, cwd, status: String
    let busy: Bool
    let action: String?
    let since: Date?
    let agents: [Span]
}

/// Official usage limits, as written by the status-line bridge.
struct LimitWindow {
    let percent: Double
    let resets: Date
}

struct Limits {
    let updated: Date
    let fiveHour, week: LimitWindow?
}

struct Snapshot {
    var loaded = false
    var liveSessions = 0, busySessions = 0
    var context = 0, contextProject = ""
    var today = Usage(), todayReplies = 0
    var window = 0
    var windowStart: Date?, windowReset: Date?
    var perMinute = 0, peakPerMinute = 0
    var lastHour: [Int] = []
    var range = TimeRange.hour
    var main: [Double] = [], agentSeries: [Double] = []  // tokens per minute, per bucket, oldest first
    var bucketSeconds: Double = 60
    var spans: [Span] = []                             // agents active in the selected range
    var heat: [[Int]] = []                             // 7 days x 24 hours, oldest day first
    var heatDays: [Date] = []
    var models: [(name: String, tokens: Int, replies: Int)] = []
    var agentsToday: [Span] = []                       // running first, then newest
    var projects: [(name: String, tokens: Int)] = []
    var now: [NowSession] = []                         // live sessions: busy first, then latest action
    var limits: Limits?
}

final class Store {
    static let retention: TimeInterval = 7 * 86400
    private let home = FileManager.default.homeDirectoryForCurrentUser
    private lazy var projectsDir = home.appendingPathComponent(".claude/projects")
    private lazy var sessionsDir = home.appendingPathComponent(".claude/sessions")
    private var transcripts: [String: Transcript] = [:]
    private var replies: [String: Reply] = [:] // keyed by message id: streamed replies repeat the same id
    private var lastDiscovery = Date.distantPast
    private let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    func refresh() {
        let now = Date()
        let cutoff = now.addingTimeInterval(-Store.retention)
        if now.timeIntervalSince(lastDiscovery) > 3 {
            discover(since: cutoff)
            lastDiscovery = now
        }
        for (path, t) in transcripts {
            let mtime = (try? FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as? Date) ?? nil
            guard let mtime, mtime != t.mtime else { continue }
            t.mtime = mtime
            read(path, t, cutoff: cutoff)
        }
        replies = replies.filter { $0.value.time >= cutoff }
    }

    private func discover(since cutoff: Date) {
        guard let e = FileManager.default.enumerator(at: projectsDir, includingPropertiesForKeys: [.contentModificationDateKey],
                                                     options: [.skipsHiddenFiles]) else { return }
        for case let url as URL in e where url.pathExtension == "jsonl" {
            let path = url.path
            guard transcripts[path] == nil,
                  let m = try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate,
                  m >= cutoff else { continue }
            let isAgent = url.deletingLastPathComponent().lastPathComponent == "subagents"
            let t = Transcript(isAgent: isAgent)
            // <project-dir>/<sessionId>.jsonl, or <project-dir>/<sessionId>/subagents/agent-*.jsonl
            t.session = isAgent ? url.deletingLastPathComponent().deletingLastPathComponent().lastPathComponent
                                : url.deletingPathExtension().lastPathComponent
            if isAgent,
               let data = try? Data(contentsOf: url.deletingPathExtension().appendingPathExtension("meta.json")),
               let meta = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                t.agentType = meta["agentType"] as? String ?? "agent"
                t.agentTask = meta["description"] as? String ?? ""
            }
            transcripts[path] = t
        }
    }

    private func read(_ path: String, _ t: Transcript, cutoff: Date) {
        guard let fh = FileHandle(forReadingAtPath: path) else { return }
        defer { try? fh.close() }
        let size = (try? fh.seekToEnd()) ?? 0
        if size < t.offset { t.offset = 0; t.partial = Data() } // file was rewritten
        try? fh.seek(toOffset: t.offset)
        guard let chunk = try? fh.readToEnd(), !chunk.isEmpty else { return }
        t.offset += UInt64(chunk.count)
        var data = t.partial + chunk
        guard let lastNewline = data.lastIndex(of: 0x0A) else { t.partial = data; return }
        t.partial = data.suffix(from: data.index(after: lastNewline))
        data = data.prefix(upTo: lastNewline)
        for line in data.split(separator: 0x0A) {
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            if t.project == "?", let cwd = obj["cwd"] as? String { t.project = Store.projectName(cwd) } // where the session started
            let lineTime = (obj["timestamp"] as? String).flatMap { iso.date(from: $0) }
            noteAction(obj, t, lineTime)
            guard obj["type"] as? String == "assistant",
                  let msg = obj["message"] as? [String: Any],
                  let u = msg["usage"] as? [String: Any],
                  let model = msg["model"] as? String, model != "<synthetic>",
                  let ts = obj["timestamp"] as? String, let time = iso.date(from: ts) else { continue }
            let usage = Usage(input: u["input_tokens"] as? Int ?? 0,
                              cacheWrite: u["cache_creation_input_tokens"] as? Int ?? 0,
                              cacheRead: u["cache_read_input_tokens"] as? Int ?? 0,
                              output: u["output_tokens"] as? Int ?? 0)
            t.lastStop = msg["stop_reason"] as? String
            t.lastContext = usage.input + usage.cacheRead + usage.cacheWrite
            t.lastReply = time
            guard time >= cutoff else { continue }
            let id = msg["id"] as? String ?? obj["uuid"] as? String ?? UUID().uuidString
            replies[id] = Reply(time: time, model: model, usage: usage, file: path)
        }
    }

    /// Updates a transcript's current action from one user or assistant line.
    private func noteAction(_ obj: [String: Any], _ t: Transcript, _ time: Date?) {
        let type = obj["type"] as? String
        guard type == "assistant" || type == "user", obj["isMeta"] as? Bool != true,
              obj["isCompactSummary"] as? Bool != true,
              let msg = obj["message"] as? [String: Any] else { return }
        if let text = msg["content"] as? String, text.hasPrefix("<local-command") || text.hasPrefix("<command-") {
            return  // slash-command noise and compact summaries
        }
        func set(_ action: String) {
            t.action = action
            t.actionTime = time
        }
        let done = t.isAgent ? "Finished" : "Waiting for you"
        if type == "assistant" {
            // non-object items are skipped; the rest still counts
            let content = (msg["content"] as? [Any])?.compactMap { $0 as? [String: Any] } ?? []
            let blocks = content.filter { $0["type"] as? String != "thinking" }
            if let use = content.last(where: { $0["type"] as? String == "tool_use" }) {
                set(Store.toolAction(use["name"] as? String ?? "", use["input"] as? [String: Any] ?? [:]))
            } else if !blocks.isEmpty, blocks.allSatisfy({ $0["type"] as? String == "text" }),
                      msg["stop_reason"] as? String == "end_turn" || msg["model"] as? String == "<synthetic>" {
                set(done)  // a synthetic reply (session limit, API error) also ends the turn
            } else {
                set("Thinking…")
            }
            return
        }
        // user line: an interruption, tool results, or a prompt
        let content = msg["content"] as? [[String: Any]] ?? []
        if Store.userText(msg["content"]).contains("[Request interrupted by user") {
            set(done)
        } else if !content.isEmpty, content.allSatisfy({ $0["type"] as? String == "tool_result" }) {
            // the tool just returned: keep its action until the next assistant line
        } else {
            set("Thinking…")
        }
    }

    /// All text in a user message's content: a string, or its text blocks and tool results.
    static func userText(_ content: Any?) -> String {
        if let s = content as? String { return s }
        guard let blocks = content as? [[String: Any]] else { return "" }
        return blocks.map { b in
            if let text = b["text"] as? String { return text }
            return userText(b["content"])
        }.joined(separator: "\n")
    }

    /// Describes a tool call in a few words. Every value taken from the transcript is untrusted and clipped.
    static func toolAction(_ name: String, _ input: [String: Any]) -> String {
        func value(_ s: String?) -> String? {
            let clean = sanitize(s ?? "", 40)
            return clean.isEmpty ? nil : clean
        }
        func field(_ key: String) -> String? { value(input[key] as? String) }
        func file(_ key: String) -> String? { value((input[key] as? String).map(basename)) }
        let found: String?
        switch name {
        case "Edit", "MultiEdit": found = file("file_path").map { "Editing \($0)" }
        case "NotebookEdit": found = file("notebook_path").map { "Editing \($0)" }
        case "Write": found = file("file_path").map { "Writing \($0)" }
        case "Read": found = file("file_path").map { "Reading \($0)" }
        case "Bash":
            let firstLine = (input["command"] as? String)?.split(whereSeparator: \.isNewline).first.map(String.init)
            found = (field("description") ?? value(firstLine)).map { "Running \($0)" }
        case "Grep": found = field("pattern").map { "Searching for \($0)" }
        case "Glob": found = field("pattern").map { "Finding \($0)" }
        case "Agent", "Task": found = (field("description") ?? field("subagent_type")).map { "Starting agent: \($0)" }
        case "WebFetch": found = value((input["url"] as? String).flatMap { URL(string: $0)?.host }).map { "Reading \($0)" }
        case "WebSearch": found = field("query").map { "Searching the web: \($0)" }
        case "TodoWrite": found = "Updating the plan"
        case "Skill": found = field("skill").map { "Using skill \($0)" }
        default:
            // mcp__<server>__<tool>
            let parts = name.hasPrefix("mcp__") ? name.dropFirst(5).components(separatedBy: "__") : []
            if parts.count >= 2, let server = value(parts[0]), let tool = value(parts.dropFirst().joined(separator: "__")) {
                found = "Using \(server): \(tool)"
            } else {
                found = nil
            }
        }
        return found ?? "Using \(value(name) ?? "a tool")"
    }

    /// A project's folder name. Agents in git worktrees run inside `<project>/.claude/worktrees/<name>`,
    /// so those count toward the project they belong to.
    static func projectName(_ cwd: String) -> String {
        let root = cwd.components(separatedBy: "/.claude/worktrees/").first ?? cwd
        return basename(root)
    }

    /// Sessions whose process is alive, with what `~/.claude/sessions/<pid>.json` says about them.
    private func liveSessions() -> [[String: Any]] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: sessionsDir.path)) ?? []
        return names.filter { $0.hasSuffix(".json") }.compactMap { name -> [String: Any]? in
            guard let pid = pid_t(name.dropLast(5)), kill(pid, 0) == 0 || errno == EPERM else { return nil }
            let data = try? Data(contentsOf: sessionsDir.appendingPathComponent(name))
            return data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        }
    }

    /// `~/.claude/claudemon/limits.json`, written by the status-line bridge. Nil when absent, invalid or expired.
    private func readLimits(now: Date) -> Limits? {
        let path = home.appendingPathComponent(".claude/claudemon/limits.json").path
        guard let fh = FileHandle(forReadingAtPath: path) else { return nil }
        defer { try? fh.close() }
        guard let data = try? fh.read(upToCount: 65537), data.count <= 65536,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Store.number(obj["version"]) == 1,
              let updated = Store.number(obj["updated_at"]),
              updated > 0, updated <= now.timeIntervalSince1970 + 60 else { return nil }
        var windows: [LimitWindow?] = []
        for key in ["five_hour", "seven_day"] {
            guard let raw = obj[key], !(raw is NSNull) else { windows.append(nil); continue }
            guard let w = raw as? [String: Any],
                  let pct = Store.number(w["used_percentage"]), (0...100).contains(pct),
                  let resets = Store.number(w["resets_at"]), resets > 0 else { return nil }
            let window = LimitWindow(percent: pct, resets: Date(timeIntervalSince1970: resets))
            // a window counts only until it resets, and never more than 8 days ahead
            let ahead = window.resets.timeIntervalSince(now)
            windows.append(ahead > 0 && ahead <= 8 * 86400 ? window : nil)
        }
        guard windows.contains(where: { $0 != nil }) else { return nil }
        return Limits(updated: Date(timeIntervalSince1970: updated), fiveHour: windows[0], week: windows[1])
    }

    /// A finite JSON number (not a boolean).
    static func number(_ v: Any?) -> Double? {
        guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite else { return nil }
        return n.doubleValue
    }

    func snapshot(range: TimeRange) -> Snapshot {
        let now = Date()
        let cal = Calendar.current
        var s = Snapshot()
        s.loaded = true
        s.range = range
        let live = liveSessions()
        s.liveSessions = live.count
        s.limits = readLimits(now: now)

        let mains = transcripts.filter { !$0.value.isAgent }
        if let current = mains.values.filter({ $0.lastReply != nil }).max(by: { $0.mtime < $1.mtime }) {
            s.context = current.lastContext
            s.contextProject = current.project
        }

        let startOfDay = cal.startOfDay(for: now)
        let all = replies.values.sorted { $0.time < $1.time }

        // Per-transcript totals, used for the agent rows and timeline.
        var perFile: [String: (first: Date, last: Date, tokens: Int, model: String)] = [:]
        for r in all {
            if var f = perFile[r.file] {
                f.last = r.time; f.tokens += r.usage.total; f.model = r.model
                perFile[r.file] = f
            } else {
                perFile[r.file] = (r.time, r.time, r.usage.total, r.model)
            }
        }

        var byModel: [String: (tokens: Int, replies: Int)] = [:], byProject: [String: Int] = [:]
        for r in all where r.time >= startOfDay {
            s.today += r.usage
            s.todayReplies += 1
            let fam = family(r.model)
            let cur = byModel[fam] ?? (0, 0)
            byModel[fam] = (cur.tokens + r.usage.total, cur.replies + 1)
            byProject[transcripts[r.file]?.project ?? "?", default: 0] += r.usage.total
        }
        s.models = byModel.map { (name: $0.key, tokens: $0.value.tokens, replies: $0.value.replies) }.sorted { $0.tokens > $1.tokens }
        s.projects = byProject.map { (name: $0.key, tokens: $0.value) }.sorted { $0.tokens > $1.tokens }

        // Estimated 5-hour window: starts at the first reply after a gap of 5h or more.
        var windowStart: Date?
        for r in all where windowStart == nil || r.time >= windowStart!.addingTimeInterval(5 * 3600) {
            windowStart = r.time
        }
        if let ws = windowStart, now < ws.addingTimeInterval(5 * 3600) {
            s.windowStart = ws
            s.windowReset = ws.addingTimeInterval(5 * 3600)
            s.window = all.filter { $0.time >= ws }.reduce(0) { $0 + $1.usage.total }
        }

        // Last hour, per minute: the Speed row and compact mode's sparkline.
        var minutes = Array(repeating: 0, count: 60)
        for r in all.reversed() {
            let age = now.timeIntervalSince(r.time)
            if age >= 3600 { break }
            if age < 0 { continue }
            minutes[59 - Int(age / 60)] += r.usage.total
            if age < 300 { s.perMinute += r.usage.total }
        }
        s.perMinute /= 5
        s.lastHour = minutes
        s.peakPerMinute = minutes.max() ?? 0

        // Chart: main session and subagents, as tokens per minute per bucket.
        if range != .week {
            let n = range.buckets
            let bucket = range.seconds / Double(n)
            var main = Array(repeating: 0.0, count: n), agents = main
            for r in all.reversed() {
                let age = now.timeIntervalSince(r.time)
                if age >= range.seconds { break }
                if age < 0 { continue }
                let i = n - 1 - min(n - 1, Int(age / bucket))
                if transcripts[r.file]?.isAgent == true { agents[i] += Double(r.usage.total) } else { main[i] += Double(r.usage.total) }
            }
            s.bucketSeconds = bucket
            s.main = main.map { $0 / (bucket / 60) }
            s.agentSeries = agents.map { $0 / (bucket / 60) }
        }

        // Heatmap: tokens per hour over the last 7 days.
        let firstDay = cal.date(byAdding: .day, value: -6, to: startOfDay) ?? startOfDay
        s.heatDays = (0..<7).map { cal.date(byAdding: .day, value: $0, to: firstDay) ?? firstDay }
        var heat = Array(repeating: Array(repeating: 0, count: 24), count: 7)
        for r in all where r.time >= firstDay {
            let d = cal.dateComponents([.day], from: firstDay, to: cal.startOfDay(for: r.time)).day ?? 0
            guard (0..<7).contains(d) else { continue }
            heat[d][cal.component(.hour, from: r.time)] += r.usage.total
        }
        s.heat = heat

        // Agents
        let spans: [Span] = transcripts.compactMap { path, t in
            guard t.isAgent, let f = perFile[path] else { return nil }
            let running = now.timeIntervalSince(t.mtime) < 90 && t.lastStop != "end_turn"
            return Span(id: path, type: t.agentType, task: t.agentTask, family: family(f.model),
                        start: f.first, end: running ? now : f.last, tokens: f.tokens, running: running,
                        session: t.session, action: t.action)
        }
        s.agentsToday = spans.filter { $0.end >= startOfDay }
            .sorted { ($0.running ? 1 : 0, $0.start) > ($1.running ? 1 : 0, $1.start) }
        if range != .week {
            let from = now.addingTimeInterval(-range.seconds)
            s.spans = spans.filter { $0.end >= from }.sorted { $0.start < $1.start }
        }

        // Now: each live session, what it is doing, and the agents it is running.
        var mainById: [String: Transcript] = [:]
        for t in mains.values where mainById[t.session].map({ $0.mtime < t.mtime }) ?? true { mainById[t.session] = t }
        s.now = live.map { info in
            let id = info["sessionId"] as? String ?? ""
            let t = id.isEmpty ? nil : mainById[id]
            let status = (info["status"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            let cwd = info["cwd"] as? String ?? ""
            let busy = status.map { $0 == "busy" } ?? t.map { now.timeIntervalSince($0.mtime) < 20 } ?? false
            let project = sanitize(cwd.isEmpty ? t?.project ?? "?" : Store.projectName(cwd), 200)
            return NowSession(project: project.isEmpty ? "?" : project,
                              name: sanitize(info["name"] as? String ?? "", 200), cwd: sanitize(cwd, 200),
                              status: sanitize(status ?? "", 200), busy: busy,
                              action: t?.action, since: t?.actionTime,
                              agents: spans.filter { $0.running && !id.isEmpty && $0.session == id }.sorted { $0.start > $1.start })
        }.sorted { ($0.busy ? 1 : 0, $0.since ?? .distantPast) > ($1.busy ? 1 : 0, $1.since ?? .distantPast) }
        s.busySessions = s.now.filter { $0.busy }.count  // same rule as the Now dots
        return s
    }

    private func family(_ model: String) -> String {
        for f in ["opus", "sonnet", "haiku", "fable"] where model.contains(f) { return f }
        return "other"
    }
}

// MARK: - Theme

struct Palette {
    let bg, border, fg, agents, dim, label, text, warn, hot, cyan, purple, tooltipBg: NSColor
}

enum ThemeChoice: Int, CaseIterable {
    case terminal, claude, system
    var title: String { ["Terminal", "Claude", "Match System"][rawValue] }
}

enum Theme {
    static var choice = ThemeChoice.terminal

    static func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: r, green: g, blue: b, alpha: a)
    }

    static let terminal = Palette(
        bg: rgb(0.035, 0.045, 0.06, 0.985), border: rgb(0.85, 0.47, 0.34, 0.55), fg: rgb(0.55, 1.00, 0.62),
        agents: rgb(1.00, 0.62, 0.27), dim: rgb(0.33, 0.40, 0.45), label: rgb(0.93, 0.56, 0.40), text: rgb(0.86, 0.90, 0.93),
        warn: rgb(1.00, 0.78, 0.25), hot: rgb(1.00, 0.33, 0.33), cyan: rgb(0.35, 0.85, 0.95), purple: rgb(0.72, 0.58, 1.00),
        tooltipBg: rgb(0.10, 0.12, 0.15))
    static let claude = Palette(
        bg: rgb(0.10, 0.094, 0.086, 0.985), border: rgb(0.85, 0.47, 0.34, 0.70), fg: rgb(0.85, 0.47, 0.34),
        agents: rgb(0.91, 0.76, 0.54), dim: rgb(0.46, 0.43, 0.40), label: rgb(0.80, 0.72, 0.64), text: rgb(0.93, 0.90, 0.86),
        warn: rgb(0.95, 0.76, 0.30), hot: rgb(0.95, 0.36, 0.33), cyan: rgb(0.45, 0.78, 0.85), purple: rgb(0.70, 0.60, 0.95),
        tooltipBg: rgb(0.16, 0.15, 0.14))
    static let light = Palette(
        bg: rgb(0.972, 0.976, 0.968, 0.985), border: rgb(0.78, 0.45, 0.33, 0.55), fg: rgb(0.12, 0.55, 0.31),
        agents: rgb(0.85, 0.47, 0.02), dim: rgb(0.47, 0.52, 0.50), label: rgb(0.75, 0.34, 0.18), text: rgb(0.11, 0.14, 0.13),
        warn: rgb(0.72, 0.47, 0.02), hot: rgb(0.77, 0.19, 0.19), cyan: rgb(0.07, 0.53, 0.66), purple: rgb(0.44, 0.28, 0.91),
        tooltipBg: rgb(1.00, 1.00, 1.00))

    static var systemIsDark: Bool { NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua }
    static var p: Palette {
        switch choice {
        case .terminal: return terminal
        case .claude: return claude
        case .system: return systemIsDark ? terminal : light
        }
    }

    static var bg: NSColor { p.bg }
    static var border: NSColor { p.border }
    static var fg: NSColor { p.fg }
    static var agents: NSColor { p.agents }
    static var dim: NSColor { p.dim }
    static var label: NSColor { p.label }
    static var text: NSColor { p.text }
    static var warn: NSColor { p.warn }
    static var hot: NSColor { p.hot }
    static var cyan: NSColor { p.cyan }
    static var purple: NSColor { p.purple }

    static let font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
    static let bold = NSFont.monospacedSystemFont(ofSize: 11, weight: .bold)
    static let small = NSFont.monospacedSystemFont(ofSize: 9.5, weight: .regular)
    static let smallBold = NSFont.monospacedSystemFont(ofSize: 9.5, weight: .semibold)

    static func model(_ family: String) -> NSColor {
        switch family {
        case "opus": return purple
        case "sonnet": return fg
        case "haiku": return cyan
        case "fable": return warn
        default: return dim
        }
    }

    static var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
}

// MARK: - Text helpers

final class Line {
    let s = NSMutableAttributedString()
    var tip: [String] = []
    let alpha: CGFloat

    init(alpha: CGFloat = 1) { self.alpha = alpha }

    @discardableResult
    func add(_ text: String, _ color: NSColor = Theme.text, bold: Bool = false) -> Line {
        let c = alpha < 1 ? color.withAlphaComponent(color.alphaComponent * alpha) : color
        s.append(NSAttributedString(string: text, attributes: [.foregroundColor: c, .font: bold ? Theme.bold : Theme.font]))
        return self
    }

    @discardableResult
    func label(_ text: String) -> Line { add(text.padding(toLength: 10, withPad: " ", startingAt: 0), Theme.label, bold: true) }

    @discardableResult
    func tip(_ lines: [String]) -> Line { tip = lines; return self }
}

let sparks = Array("▁▂▃▄▅▆▇█")

func tokens(_ n: Int) -> String {
    let d = Double(n)
    if n >= 1_000_000 { return String(format: "%.1fM", d / 1_000_000) }
    if n >= 10_000 { return String(format: "%.0fk", d / 1000) }
    if n >= 1000 { return String(format: "%.1fk", d / 1000) }
    return "\(n)"
}

let exactFormatter: NumberFormatter = { let f = NumberFormatter(); f.numberStyle = .decimal; return f }()
func exact(_ n: Int) -> String { exactFormatter.string(from: NSNumber(value: n)) ?? "\(n)" }

func clip(_ s: String, _ n: Int) -> String {
    s.count <= n ? s.padding(toLength: n, withPad: " ", startingAt: 0) : String(s.prefix(n - 1)) + "…"
}

/// Untrusted text for one line: control, format and separator characters (Cc, Cf, Zl, Zp: bidi overrides,
/// zero-width characters) and all whitespace become single spaces, trimmed, clipped to n scalars with "…".
func sanitize(_ s: String, _ n: Int) -> String {
    var out = String.UnicodeScalarView()
    for u in s.unicodeScalars {
        switch u.properties.generalCategory {
        case .control, .format, .lineSeparator, .paragraphSeparator: break
        default:
            if !u.properties.isWhitespace { out.append(u); continue }
        }
        if !out.isEmpty && out.last != " " { out.append(" ") }
    }
    if out.last == " " { out.removeLast() }
    return out.count <= n ? String(out) : String(String.UnicodeScalarView(out.prefix(n - 1))) + "…"
}

/// The last path component, splitting on both "/" and "\\".
func basename(_ path: String) -> String {
    String(path.split(whereSeparator: { $0 == "/" || $0 == "\\" }).last ?? "")
}

func duration(_ seconds: TimeInterval) -> String {
    let s = Int(max(0, seconds))
    return s >= 3600 ? "\(s / 3600)h\(s % 3600 / 60)m" : s >= 60 ? "\(s / 60)m\(s % 60)s" : "\(s)s"
}

func text(_ s: String, _ color: NSColor, _ font: NSFont = Theme.small) -> NSAttributedString {
    NSAttributedString(string: s, attributes: [.font: font, .foregroundColor: color])
}

// MARK: - View

final class MonitorView: NSView {
    var snap = Snapshot()
    var range = TimeRange.hour
    var compact = false
    var spinPhase = true
    var onRangeChange: ((TimeRange) -> Void)?
    var onToggleCompact: (() -> Void)?

    // Animation: numbers ease toward their targets; new agents fade in.
    private var shown: [String: Double] = [:]
    private var targets: [String: Double] = [:]
    private var firstSeen: [String: Date] = [:]
    private var seeded = false
    private let fadeSeconds = 0.6

    // Hit regions, rebuilt on every draw.
    private var rowRegions: [(NSRect, [String])] = []
    private var segmentRects: [(NSRect, TimeRange)] = []
    private var chartRect = NSRect.zero
    private var spanRegions: [(NSRect, Span)] = []
    private var heatRegions: [(NSRect, Int, Int)] = []
    private var mouse: NSPoint?

    let clock: DateFormatter = { let f = DateFormatter(); f.dateFormat = "HH:mm:ss"; return f }()
    let hm: DateFormatter = { let f = DateFormatter(); f.dateFormat = "HH:mm"; return f }()
    let dayName: DateFormatter = { let f = DateFormatter(); f.dateFormat = "EEE"; return f }()
    let dayLong: DateFormatter = { let f = DateFormatter(); f.dateFormat = "EEE d MMM"; return f }()
    let dayHM: DateFormatter = { let f = DateFormatter(); f.dateFormat = "EEE HH:mm"; return f }()
    let fullTime: DateFormatter = { let f = DateFormatter(); f.dateFormat = "EEE d MMM HH:mm:ss"; return f }()

    let pad = NSSize(width: 14, height: 10)
    let titleHeight: CGFloat = 24
    let gap: CGFloat = 12, headerH: CGFloat = 20, subH: CGFloat = 14, plotH: CGFloat = 56, axisH: CGFloat = 14
    let laneH: CGFloat = 7, laneGap: CGFloat = 3, maxLanes = 6
    let cellH: CGFloat = 10, cellGap: CGFloat = 2
    lazy var rowH: CGFloat = ceil(Line().add("Xy").s.size().height) + 3

    override var isFlipped: Bool { true }

    // MARK: State updates

    func apply(_ s: Snapshot) {
        let now = Date()
        if s.loaded && !seeded {
            // Agents that already existed at launch appear without a fade.
            for a in s.agentsToday { firstSeen[a.id] = .distantPast }
            seeded = true
        }
        for a in s.agentsToday where firstSeen[a.id] == nil { firstSeen[a.id] = Theme.reduceMotion ? .distantPast : now }
        snap = s
        targets = ["today": Double(s.today.total), "window": Double(s.window), "speed": Double(s.perMinute),
                   "peak": Double(s.peakPerMinute), "ctx": Double(s.context)]
        for (k, v) in targets where shown[k] == nil || Theme.reduceMotion { shown[k] = v }
    }

    /// Advances animations by one frame. Returns true while anything is still moving.
    func stepAnimation() -> Bool {
        var moving = false
        for (k, target) in targets {
            let cur = shown[k] ?? target
            let diff = target - cur
            if abs(diff) <= max(1, abs(target) * 0.002) {
                shown[k] = target
            } else {
                shown[k] = cur + diff * 0.25
                moving = true
            }
        }
        let now = Date()
        if firstSeen.values.contains(where: { now.timeIntervalSince($0) < fadeSeconds }) { moving = true }
        return moving
    }

    var needsAnimation: Bool {
        if Theme.reduceMotion { return false }
        let now = Date()
        return targets.contains { abs(($0.value) - (shown[$0.key] ?? $0.value)) > max(1, abs($0.value) * 0.002) }
            || firstSeen.values.contains { now.timeIntervalSince($0) < fadeSeconds }
    }

    private func num(_ key: String, _ fallback: Int) -> Int { Int((shown[key] ?? Double(fallback)).rounded()) }

    private func fade(_ id: String) -> CGFloat {
        guard let seen = firstSeen[id] else { return 1 }
        return CGFloat(min(1, Date().timeIntervalSince(seen) / fadeSeconds))
    }

    var runningAgents: Int { snap.agentsToday.filter(\.running).count }
    var showTimeline: Bool { range != .week && !snap.spans.isEmpty }
    var hasAgentSeries: Bool { snap.agentSeries.contains { $0 > 0 } }

    // MARK: Rows

    func rows() -> [Line] {
        let s = snap
        guard s.loaded else { return [Line().add("Reading Claude Code logs…", Theme.dim)] }
        var lines: [Line] = []

        let ctx = num("ctx", s.context)
        let ses = Line().label("Sessions").add("\(s.liveSessions)", Theme.fg, bold: true).add(" live", Theme.dim)
        if s.busySessions > 0 { ses.add(" · ", Theme.dim).add("\(s.busySessions) busy", Theme.warn) }
        if ctx > 0 {
            ses.add(" · ctx ", Theme.dim).add(tokens(ctx)).add(" \(clip(s.contextProject, 16).trimmingCharacters(in: .whitespaces))", Theme.dim)
        }
        ses.tip(["Sessions",
                 "\(s.liveSessions) running now, \(s.busySessions) busy",
                 s.context > 0 ? "Latest conversation: \(exact(s.context)) tokens of context (\(s.contextProject))" : "No conversation yet"])
        lines.append(ses)
        lines += nowRows()

        let usageTip = ["Tokens today (\(exact(s.todayReplies)) replies)",
                        "Input:        \(exact(s.today.input))",
                        "Cache writes: \(exact(s.today.cacheWrite))",
                        "Cache reads:  \(exact(s.today.cacheRead))",
                        "Output:       \(exact(s.today.output))",
                        s.windowStart.map { "5h window since \(hm.string(from: $0)) (estimate): \(exact(s.window))" } ?? "No active 5h window"]
        let tok = Line().label("Tokens").add("today ", Theme.dim).add(tokens(num("today", s.today.total)), Theme.fg, bold: true)
        if let reset = s.windowReset {
            tok.add("  5h ", Theme.dim).add(tokens(num("window", s.window)), Theme.fg, bold: true).add(" · resets ~\(hm.string(from: reset))", Theme.dim)
        }
        lines.append(tok.tip(usageTip))
        lines.append(Line().add("          ").add("in ", Theme.dim).add(tokens(s.today.input + s.today.cacheWrite))
            .add("  out ", Theme.dim).add(tokens(s.today.output))
            .add("  cache ", Theme.dim).add(tokens(s.today.cacheRead)).tip(usageTip))
        if let limits = s.limits { lines.append(limitsRow(limits)) }

        let speed = num("speed", s.perMinute)
        lines.append(Line().label("Speed").add(tokens(speed), speed > 0 ? Theme.fg : Theme.dim, bold: true)
            .add("/min now", Theme.dim).add(" · peak ", Theme.dim).add(tokens(num("peak", s.peakPerMinute))).add("/min", Theme.dim)
            .tip(["Speed",
                  "Now: \(exact(s.perMinute)) tokens/min (average of the last 5 minutes)",
                  "Peak: \(exact(s.peakPerMinute)) tokens/min (busiest minute in the last hour)"]))

        let total = max(1, s.models.reduce(0) { $0 + $1.tokens })
        let mdl = Line().label("Models")
        if s.models.isEmpty {
            mdl.add("no replies today", Theme.dim)
        } else {
            var used = 0
            for (i, m) in s.models.enumerated() {
                let w = i == s.models.count - 1 ? 18 - used : max(1, m.tokens * 18 / total)
                mdl.add(String(repeating: "█", count: max(0, w)), Theme.model(m.name))
                used += w
            }
            for m in s.models.prefix(3) {
                mdl.add(" \(m.name) ", Theme.model(m.name)).add(m.tokens * 100 < total ? "<1%" : "\(m.tokens * 100 / total)%", Theme.dim)
            }
        }
        lines.append(mdl.tip(["Models today"] + s.models.map {
            "\($0.name): \(exact($0.tokens)) tokens · \($0.replies) replies · \($0.tokens * 100 < total ? "<1" : "\($0.tokens * 100 / total)")%"
        }))

        lines.append(Line().label("Agents").add("\(runningAgents)", runningAgents > 0 ? Theme.warn : Theme.dim, bold: true)
            .add(" running", Theme.dim).add(" · ", Theme.dim).add("\(s.agentsToday.count)").add(" today", Theme.dim)
            .tip(["Subagents", "\(runningAgents) running now", "\(s.agentsToday.count) run today"]))
        for a in s.agentsToday.prefix(4) {
            let icon = a.running ? (spinPhase ? "◐ " : "◓ ") : "✓ "
            let row = Line(alpha: fade(a.id)).add("  ").add(icon, a.running ? Theme.warn : Theme.fg)
                .add(clip(a.type, 15), a.running ? Theme.text : Theme.dim)
                .add(" " + clip(a.task, 22), Theme.dim)
                .add(" " + tokens(a.tokens).padding(toLength: 6, withPad: " ", startingAt: 0), a.running ? Theme.text : Theme.dim)
                .add(a.running ? duration(Date().timeIntervalSince(a.start)) : "", Theme.warn)
            lines.append(row.tip(agentTip(a)))
        }

        if !s.projects.isEmpty {
            let top = Line().label("Projects")
            for (i, p) in s.projects.prefix(2).enumerated() {
                if i > 0 { top.add(" · ", Theme.dim) }
                top.add(clip(p.name, 16).trimmingCharacters(in: .whitespaces)).add(" \(tokens(p.tokens))", Theme.dim)
            }
            lines.append(top.tip(["Projects today"] + s.projects.prefix(6).map { "\($0.name): \(exact($0.tokens)) tokens" }))
        }
        return lines
    }

    private func agentTip(_ a: Span) -> [String] {
        [a.type, a.task.isEmpty ? "(no description)" : a.task,
         "Model: \(a.family) · \(exact(a.tokens)) tokens",
         a.running ? "Running for \(duration(Date().timeIntervalSince(a.start)))"
                   : "Ran \(hm.string(from: a.start))–\(hm.string(from: a.end)) (\(duration(a.end.timeIntervalSince(a.start))))"]
    }

    /// One line per live session (at most 3), each followed by its running agents (at most 3).
    private func nowRows() -> [Line] {
        let now = Date()
        var lines: [Line] = []
        for n in snap.now.prefix(3) {
            let row = Line().add("  ").add("● ", n.busy ? Theme.warn : Theme.dim)
                .add(sanitize(n.project, 16), Theme.text)
            if let action = n.action {
                row.add(" · ", Theme.dim).add(sanitize(action, 48), Theme.dim)
            }
            if let since = n.since { row.add(" · ", Theme.dim).add(duration(now.timeIntervalSince(since)), Theme.dim) }
            var tip = [n.project]
            if !n.name.isEmpty { tip.append("Session: \(n.name)") }
            if !n.cwd.isEmpty { tip.append(n.cwd) }
            tip.append("Status: \(n.status.isEmpty ? (n.busy ? "busy" : "idle") : n.status)")
            if let action = n.action { tip.append(action) }
            if let since = n.since { tip.append("since \(clock.string(from: since))") }
            lines.append(row.tip(tip))

            for (i, a) in n.agents.prefix(3).enumerated() {
                let last = i == n.agents.count - 1
                let agent = Line(alpha: fade(a.id)).add(last ? "    └─ " : "    ├─ ", Theme.dim)
                    .add(spinPhase ? "◐ " : "◓ ", Theme.warn)
                    .add(sanitize(a.type, 20), Theme.text)
                agent.add(a.action.map { ": \(sanitize($0, 40)) · " } ?? " · ", Theme.dim).add(duration(now.timeIntervalSince(a.start)), Theme.warn)
                lines.append(agent.tip(agentTip(a) + (a.action.map { ["Now: \($0)"] } ?? [])))
            }
            if n.agents.count > 3 { lines.append(Line().add("    └─ +\(n.agents.count - 3) more", Theme.dim)) }
        }
        if snap.now.count > 3 { lines.append(Line().add("  +\(snap.now.count - 3) more sessions", Theme.dim)) }
        return lines
    }

    private func limitsRow(_ l: Limits) -> Line {
        let row = Line().label("Limits")
        var tip = ["Official Claude usage from Claude Code's status line",
                   "Updated \(Int(max(0, Date().timeIntervalSince(l.updated))))s ago"]
        let windows = [("5h", "5-hour", l.fiveHour, hm), ("week", "7-day", l.week, dayHM)]
        for (short, long, window, format) in windows {
            guard let w = window else { continue }
            let pct = Int((w.percent + 0.5).rounded(.down))   // half-up
            if row.s.length > 10 { row.add(" · ", Theme.dim) }
            row.add("\(short) ", Theme.dim).add("\(pct)%", pct >= 90 ? Theme.hot : pct >= 70 ? Theme.warn : Theme.fg, bold: true)
                .add(" · resets \(format.string(from: w.resets))", Theme.dim)
            tip.append("\(long): \(String(format: "%g", w.percent))% used, resets \(fullTime.string(from: w.resets))")
        }
        return row.tip(tip)
    }

    func compactSummary() -> NSAttributedString {
        let l = Line()
        guard snap.loaded else { return l.add("loading…", Theme.dim).s }
        l.add(tokens(num("speed", snap.perMinute)), Theme.fg, bold: true).add("/min ", Theme.dim)
        let recent = snap.lastHour.suffix(20)
        let peak = max(1, recent.max() ?? 1)
        for v in recent { l.add(v == 0 ? "·" : String(sparks[min(7, v * 8 / (peak + 1))]), v == 0 ? Theme.dim : Theme.fg) }
        l.add("  \(snap.liveSessions) live", Theme.dim)
        if runningAgents > 0 { l.add("  \(spinPhase ? "◐" : "◓") \(runningAgents) agent\(runningAgents == 1 ? "" : "s")", Theme.warn) }
        return l.s
    }

    // MARK: Layout

    private func lanes() -> [(Span, Int)] {
        var ends: [Date] = []
        var out: [(Span, Int)] = []
        for s in snap.spans {
            if let free = ends.firstIndex(where: { $0 < s.start }) {
                ends[free] = s.end; out.append((s, free))
            } else if ends.count < maxLanes {
                ends.append(s.end); out.append((s, ends.count - 1))
            } else {
                let i = ends.indices.min { ends[$0] < ends[$1] } ?? 0
                ends[i] = max(ends[i], s.end); out.append((s, i))
            }
        }
        return out
    }

    private var chartBlockHeight: CGFloat {
        gap + headerH + (range == .week ? 7 * (cellH + cellGap) + axisH : subH + plotH + axisH)
    }

    private var timelineHeight: CGFloat {
        let n = (lanes().map(\.1).max() ?? -1) + 1
        return gap + headerH + CGFloat(n) * (laneH + laneGap)
    }

    private let dots = Line().add("● ", Theme.hot).add("● ", Theme.warn).add("●", Theme.rgb(0.35, 0.80, 0.45)).s

    func fittingSize() -> NSSize {
        if compact {
            return NSSize(width: 10 + ceil(dots.size().width) + 12 + ceil(compactSummary().size().width) + 12, height: titleHeight)
        }
        let lines = rows()
        let width = lines.map { ceil($0.s.size().width) }.max() ?? 0
        var height = titleHeight + pad.height + CGFloat(lines.count) * rowH + chartBlockHeight + pad.height
        if showTimeline { height += timelineHeight }
        return NSSize(width: max(width + pad.width * 2 + 8, 400), height: height)
    }

    // MARK: Drawing

    override func draw(_ dirtyRect: NSRect) {
        let shape = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 9, yRadius: 9)
        Theme.bg.setFill()
        shape.fill()

        NSGraphicsContext.saveGraphicsState()
        shape.addClip()
        Theme.border.withAlphaComponent(0.07).setFill()
        NSRect(x: 0, y: 0, width: bounds.width, height: titleHeight).fill()
        NSGraphicsContext.restoreGraphicsState()

        // Border: glows while agents are running.
        let glowing = runningAgents > 0
        if glowing {
            let inner = NSBezierPath(roundedRect: bounds.insetBy(dx: 2, dy: 2), xRadius: 8, yRadius: 8)
            Theme.agents.withAlphaComponent(Theme.reduceMotion || spinPhase ? 0.35 : 0.15).setStroke()
            inner.lineWidth = 3
            inner.stroke()
        }
        (glowing ? Theme.agents.withAlphaComponent(0.9) : Theme.border).setStroke()
        shape.lineWidth = 1
        shape.stroke()

        dots.draw(at: NSPoint(x: 10, y: (titleHeight - dots.size().height) / 2))
        if compact {
            let summary = compactSummary()
            summary.draw(at: NSPoint(x: 10 + dots.size().width + 12, y: (titleHeight - summary.size().height) / 2))
            return
        }
        Theme.border.withAlphaComponent(0.3).setFill()
        NSRect(x: 0, y: titleHeight, width: bounds.width, height: 1).fill()
        let title = text("claudemon", Theme.dim, Theme.font)
        title.draw(at: NSPoint(x: 10 + dots.size().width + 12, y: (titleHeight - title.size().height) / 2))
        let clk = text(clock.string(from: Date()), Theme.dim, Theme.font)
        clk.draw(at: NSPoint(x: bounds.width - clk.size().width - 12, y: (titleHeight - clk.size().height) / 2))

        rowRegions = []
        var y = titleHeight + pad.height
        for line in rows() {
            line.s.draw(at: NSPoint(x: pad.width, y: y))
            if !line.tip.isEmpty { rowRegions.append((NSRect(x: 0, y: y - 1, width: bounds.width, height: rowH), line.tip)) }
            y += rowH
        }

        y += gap
        drawChartHeader(top: y)
        y += headerH
        spanRegions = []
        heatRegions = []
        if range == .week {
            chartRect = .zero
            drawHeatmap(top: y)
        } else {
            drawLineChart(top: y)
            y += subH + plotH + axisH
            if showTimeline { drawTimeline(top: y + gap) }
        }
        drawTooltip()
    }

    private func drawChartHeader(top: CGFloat) {
        let left = pad.width, right = bounds.width - pad.width
        let title = range == .week ? "Activity, last 7 days" : "Tokens per minute"
        text(title, Theme.text, Theme.font).draw(at: NSPoint(x: left, y: top + 2))

        // Range switch
        segmentRects = []
        let labels = TimeRange.allCases.map { text($0.label, Theme.text, Theme.smallBold) }
        let segW = labels.map { ceil($0.size().width) + 14 }
        let total = segW.reduce(0, +)
        let box = NSRect(x: right - total, y: top + 1, width: total, height: 16)
        let container = NSBezierPath(roundedRect: box, xRadius: 5, yRadius: 5)
        Theme.dim.withAlphaComponent(0.12).setFill()
        container.fill()
        var x = box.minX
        for (i, r) in TimeRange.allCases.enumerated() {
            let seg = NSRect(x: x, y: box.minY, width: segW[i], height: box.height)
            if r == range {
                Theme.label.withAlphaComponent(0.28).setFill()
                NSBezierPath(roundedRect: seg.insetBy(dx: 1, dy: 1), xRadius: 4, yRadius: 4).fill()
            }
            let t = text(r.label, r == range ? Theme.text : Theme.dim, Theme.smallBold)
            t.draw(at: NSPoint(x: seg.midX - t.size().width / 2, y: seg.midY - t.size().height / 2))
            segmentRects.append((seg, r))
            x += segW[i]
        }
    }

    private func drawLineChart(top: CGFloat) {
        let left = pad.width, width = bounds.width - pad.width * 2
        let main = snap.main, agents = snap.agentSeries
        let peak = max(1, (main + agents).max() ?? 1)

        // Legend (only needed with two series) and scale.
        if hasAgentSeries {
            var x = left
            for (name, color) in [("Main session", Theme.fg), ("Agents", Theme.agents)] {
                color.setFill()
                NSBezierPath(ovalIn: NSRect(x: x, y: top + 4, width: 7, height: 7)).fill()
                let t = text(name, Theme.dim)
                t.draw(at: NSPoint(x: x + 10, y: top))
                x += 10 + t.size().width + 12
            }
        }
        let scale = NSMutableAttributedString(attributedString: text("max ", Theme.dim))
        scale.append(text(tokens(Int(peak)) + "/min", Theme.text))
        scale.draw(at: NSPoint(x: left + width - scale.size().width, y: top))

        let r = NSRect(x: left, y: top + subH, width: width, height: plotH)
        chartRect = r
        guard main.count > 1 else { return }

        for f in [0.0, 0.5, 1.0] as [CGFloat] {
            let y = (r.minY + 4 + (r.height - 4) * f).rounded() + 0.5
            let g = NSBezierPath()
            g.move(to: NSPoint(x: r.minX, y: y)); g.line(to: NSPoint(x: r.maxX, y: y))
            g.lineWidth = 1
            if f < 1 { g.setLineDash([2, 3], count: 2, phase: 0) }
            Theme.dim.withAlphaComponent(f < 1 ? 0.25 : 0.45).setStroke()
            g.stroke()
        }

        func point(_ data: [Double], _ i: Int) -> NSPoint {
            NSPoint(x: r.minX + r.width * CGFloat(i) / CGFloat(data.count - 1),
                    y: r.maxY - (r.height - 4) * CGFloat(data[i] / peak))
        }
        func path(_ data: [Double]) -> NSBezierPath {
            let p = NSBezierPath()
            p.move(to: point(data, 0))
            for i in 1..<data.count { p.line(to: point(data, i)) }
            p.lineWidth = 2
            p.lineJoinStyle = .round
            p.lineCapStyle = .round
            return p
        }

        // Agents first, so the main session's line stays on top where they overlap.
        if hasAgentSeries {
            Theme.agents.setStroke()
            path(agents).stroke()
        }
        let mainLine = path(main)
        let area = mainLine.copy() as! NSBezierPath
        area.line(to: NSPoint(x: r.maxX, y: r.maxY))
        area.line(to: NSPoint(x: r.minX, y: r.maxY))
        area.close()
        NSGradient(starting: Theme.fg.withAlphaComponent(0.28), ending: Theme.fg.withAlphaComponent(0))?.draw(in: area, angle: 90)
        Theme.fg.setStroke()
        mainLine.stroke()

        func dot(_ p: NSPoint, _ color: NSColor) {
            Theme.bg.setFill()
            NSBezierPath(ovalIn: NSRect(x: p.x - 6, y: p.y - 6, width: 12, height: 12)).fill()
            color.setFill()
            NSBezierPath(ovalIn: NSRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8)).fill()
        }

        let axisY = r.maxY + 3
        let labels = range.axis
        if labels.count == 3 {
            text(labels[0], Theme.dim).draw(at: NSPoint(x: r.minX, y: axisY))
            let mid = text(labels[1], Theme.dim)
            mid.draw(at: NSPoint(x: r.midX - mid.size().width / 2, y: axisY))
            let end = text(labels[2], Theme.dim)
            end.draw(at: NSPoint(x: r.maxX - end.size().width, y: axisY))
        }

        if let i = chartIndex() {
            let p = point(main, i)
            let cross = NSBezierPath()
            cross.move(to: NSPoint(x: p.x.rounded() + 0.5, y: r.minY)); cross.line(to: NSPoint(x: p.x.rounded() + 0.5, y: r.maxY))
            cross.lineWidth = 1
            Theme.text.withAlphaComponent(0.35).setStroke()
            cross.stroke()
            dot(p, Theme.fg)
            if hasAgentSeries { dot(point(agents, i), Theme.agents) }
        } else {
            dot(point(main, main.count - 1), Theme.fg)
        }
    }

    private func xFor(_ t: Date, in r: NSRect) -> CGFloat {
        let from = Date().addingTimeInterval(-range.seconds)
        let f = max(0, min(1, t.timeIntervalSince(from) / range.seconds))
        return r.minX + r.width * CGFloat(f)
    }

    private func drawTimeline(top: CGFloat) {
        let left = pad.width, right = bounds.width - pad.width
        text("Agents", Theme.text, Theme.font).draw(at: NSPoint(x: left, y: top + 2))

        // Legend: the models present, right-aligned.
        let families = Array(Set(snap.spans.map(\.family))).sorted()
        var items: [(NSAttributedString, NSColor)] = families.map { (text($0, Theme.dim), Theme.model($0)) }
        items.reverse()
        var x = right
        for (label, color) in items {
            x -= label.size().width
            label.draw(at: NSPoint(x: x, y: top + 3))
            x -= 10
            color.setFill()
            NSBezierPath(ovalIn: NSRect(x: x, y: top + 7, width: 7, height: 7)).fill()
            x -= 12
        }

        let r = NSRect(x: chartRect.minX, y: top + headerH, width: chartRect.width, height: 0)
        for (span, lane) in lanes() {
            let x0 = xFor(span.start, in: r)
            let x1 = max(x0 + 3, xFor(span.end, in: r))
            let bar = NSRect(x: x0, y: r.minY + CGFloat(lane) * (laneH + laneGap), width: x1 - x0, height: laneH)
            let color = Theme.model(span.family)
            let alpha: CGFloat = span.running && !Theme.reduceMotion ? (spinPhase ? 1 : 0.7) : 0.9
            color.withAlphaComponent(alpha * fade(span.id)).setFill()
            NSBezierPath(roundedRect: bar, xRadius: 3, yRadius: 3).fill()
            spanRegions.append((bar.insetBy(dx: -2, dy: -2), span))
        }
    }

    private func drawHeatmap(top: CGFloat) {
        let labelW: CGFloat = 30
        let x0 = pad.width + labelW
        let width = bounds.width - pad.width - x0
        let cellW = (width - 23 * cellGap) / 24
        let maxValue = max(1, snap.heat.flatMap { $0 }.max() ?? 1)
        for (d, day) in snap.heat.enumerated() {
            let y = top + CGFloat(d) * (cellH + cellGap)
            if d < snap.heatDays.count {
                let isToday = d == snap.heat.count - 1
                text(isToday ? "Today" : dayName.string(from: snap.heatDays[d]), isToday ? Theme.text : Theme.dim)
                    .draw(at: NSPoint(x: pad.width, y: y - 1))
            }
            for (h, v) in day.enumerated() {
                let cell = NSRect(x: x0 + CGFloat(h) * (cellW + cellGap), y: y, width: cellW, height: cellH)
                let color = v == 0 ? Theme.dim.withAlphaComponent(0.14)
                                   : Theme.fg.withAlphaComponent(0.22 + 0.78 * CGFloat(Double(v) / Double(maxValue)))
                color.setFill()
                NSBezierPath(roundedRect: cell, xRadius: 2, yRadius: 2).fill()
                heatRegions.append((cell.insetBy(dx: -cellGap / 2, dy: -cellGap / 2), d, h))
            }
        }
        let axisY = top + 7 * (cellH + cellGap) + 1
        for h in [0, 6, 12, 18] {
            text("\(h):00", Theme.dim).draw(at: NSPoint(x: x0 + CGFloat(h) * (cellW + cellGap), y: axisY))
        }
    }

    // MARK: Hover

    private func chartIndex() -> Int? {
        guard let m = mouse, range != .week, snap.main.count > 1,
              chartRect.insetBy(dx: -6, dy: -8).contains(m) else { return nil }
        let n = snap.main.count
        return max(0, min(n - 1, Int(((m.x - chartRect.minX) / chartRect.width * CGFloat(n - 1)).rounded())))
    }

    private func tooltipLines() -> [String]? {
        guard let m = mouse else { return nil }
        if let i = chartIndex() {
            let end = Date().addingTimeInterval(-Double(snap.main.count - 1 - i) * snap.bucketSeconds)
            let start = end.addingTimeInterval(-snap.bucketSeconds)
            var lines = [snap.bucketSeconds <= 60 ? hm.string(from: end) : "\(hm.string(from: start))–\(hm.string(from: end))",
                         "Main session: \(tokens(Int(snap.main[i])))/min"]
            if hasAgentSeries { lines.append("Agents: \(tokens(Int(snap.agentSeries[i])))/min") }
            return lines
        }
        if let (_, span) = spanRegions.last(where: { $0.0.contains(m) }) {
            return [span.type, span.task.isEmpty ? "(no description)" : span.task,
                    "Model: \(span.family) · \(exact(span.tokens)) tokens",
                    span.running ? "Running for \(duration(Date().timeIntervalSince(span.start)))"
                                 : "\(hm.string(from: span.start))–\(hm.string(from: span.end)) (\(duration(span.end.timeIntervalSince(span.start))))"]
        }
        if let (_, d, h) = heatRegions.first(where: { $0.0.contains(m) }), d < snap.heatDays.count {
            return ["\(dayLong.string(from: snap.heatDays[d])), \(String(format: "%02d", h)):00–\(String(format: "%02d", (h + 1) % 24)):00",
                    "\(exact(snap.heat[d][h])) tokens"]
        }
        if let (_, tip) = rowRegions.first(where: { $0.0.contains(m) }) { return tip }
        return nil
    }

    private func drawTooltip() {
        guard let m = mouse, let lines = tooltipLines(), !lines.isEmpty else { return }
        let body = NSMutableAttributedString()
        for (i, l) in lines.enumerated() {
            if i > 0 { body.append(text("\n", Theme.text)) }
            body.append(text(l, i == 0 ? Theme.text : Theme.dim, i == 0 ? Theme.smallBold : Theme.small))
        }
        let size = body.size()
        var box = NSRect(x: m.x + 12, y: m.y + 14, width: size.width + 14, height: size.height + 8)
        if box.maxX > bounds.width - 4 { box.origin.x = max(4, m.x - 12 - box.width) }
        if box.maxY > bounds.height - 4 { box.origin.y = max(titleHeight + 2, m.y - 10 - box.height) }
        let bubble = NSBezierPath(roundedRect: box, xRadius: 5, yRadius: 5)
        Theme.p.tooltipBg.setFill()
        bubble.fill()
        Theme.border.withAlphaComponent(0.6).setStroke()
        bubble.lineWidth = 1
        bubble.stroke()
        body.draw(at: NSPoint(x: box.minX + 7, y: box.minY + 4))
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }

    override func mouseMoved(with event: NSEvent) {
        mouse = convert(event.locationInWindow, from: nil)
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        mouse = nil
        needsDisplay = true
    }

    // MARK: Input

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        if p.y < titleHeight && event.clickCount == 2 { onToggleCompact?(); return }
        if p.y < titleHeight && p.x < 22 { NSApp.terminate(nil); return }
        if !compact, let (_, r) = segmentRects.first(where: { $0.0.contains(p) }) {
            if r != range { onRangeChange?(r) }
            return
        }
        window?.performDrag(with: event)
    }

    override func rightMouseDown(with event: NSEvent) {
        let menu = NSMenu()
        let pin = NSMenuItem(title: "Always on Top", action: #selector(AppDelegate.togglePin), keyEquivalent: "")
        pin.state = window?.level == .floating ? .on : .off
        menu.addItem(pin)
        let small = NSMenuItem(title: "Compact Mode", action: #selector(AppDelegate.toggleCompact), keyEquivalent: "c")
        small.keyEquivalentModifierMask = []
        small.state = compact ? .on : .off
        menu.addItem(small)
        let themes = NSMenu()
        for t in ThemeChoice.allCases {
            let item = NSMenuItem(title: t.title, action: #selector(AppDelegate.chooseTheme(_:)), keyEquivalent: "")
            item.tag = t.rawValue
            item.state = Theme.choice == t ? .on : .off
            themes.addItem(item)
        }
        let themeItem = NSMenuItem(title: "Theme", action: nil, keyEquivalent: "")
        themeItem.submenu = themes
        menu.addItem(themeItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit claudemon", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }

    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func keyDown(with event: NSEvent) {
        let key = event.charactersIgnoringModifiers ?? ""
        if key == "q" || event.keyCode == 53 { NSApp.terminate(nil) }
        else if key == "c" { onToggleCompact?() }
        else if let n = Int(key), let r = TimeRange(rawValue: n - 1), r != range { onRangeChange?(r) }
    }
}

// MARK: - App

final class PanelWindow: NSWindow {
    override var canBecomeKey: Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: PanelWindow!
    let view = MonitorView()
    let store = Store()
    let queue = DispatchQueue(label: "claudemon.store")
    let defaults = UserDefaults.standard
    var ticks = 0
    var animationTimer: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        Theme.choice = ThemeChoice(rawValue: defaults.integer(forKey: "theme")) ?? .terminal
        view.range = TimeRange(rawValue: defaults.integer(forKey: "range")) ?? .hour
        view.compact = defaults.bool(forKey: "compact")
        view.onRangeChange = { [weak self] r in self?.setRange(r) }
        view.onToggleCompact = { [weak self] in self?.toggleCompact() }

        let size = view.fittingSize()
        window = PanelWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.contentView = view
        if let screen = NSScreen.main?.visibleFrame {
            window.setFrameOrigin(NSPoint(x: screen.maxX - size.width - 16, y: screen.minY + 16))
        }
        window.setFrameAutosaveName("claudemon")
        keepOnScreen()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(view)

        refresh()
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.tick() }
    }

    func refresh() {
        let range = view.range
        queue.async { [weak self] in
            guard let self else { return }
            self.store.refresh()
            let snap = self.store.snapshot(range: range)
            DispatchQueue.main.async {
                guard snap.range == self.view.range else { return } // the range changed while this was computing
                self.view.apply(snap)
                self.resize()
                self.view.needsDisplay = true
                self.animateIfNeeded()
            }
        }
    }

    func tick() {
        ticks += 1
        view.spinPhase.toggle()
        if ticks % 2 == 0 { refresh() }
        view.needsDisplay = true
    }

    func animateIfNeeded() {
        guard animationTimer == nil, view.needsAnimation else { return }
        animationTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            let moving = self.view.stepAnimation()
            self.view.needsDisplay = true
            if !moving { timer.invalidate(); self.animationTimer = nil }
        }
    }

    func setRange(_ r: TimeRange) {
        view.range = r
        defaults.set(r.rawValue, forKey: "range")
        view.needsDisplay = true
        refresh()
    }

    @objc func toggleCompact() {
        view.compact.toggle()
        defaults.set(view.compact, forKey: "compact")
        resize(force: true)
        view.needsDisplay = true
    }

    @objc func chooseTheme(_ sender: NSMenuItem) {
        Theme.choice = ThemeChoice(rawValue: sender.tag) ?? .terminal
        defaults.set(Theme.choice.rawValue, forKey: "theme")
        view.needsDisplay = true
    }

    @objc func togglePin() {
        window.level = window.level == .floating ? .normal : .floating
    }

    /// Fits the window to its content, keeping the right edge and the title bar where they are.
    /// Normal updates only ever widen the window, so it doesn't jitter as numbers change.
    func resize(force: Bool = false) {
        let size = view.fittingSize()
        var f = window.frame
        let width = force ? size.width : max(size.width, f.width)
        guard force || abs(size.height - f.height) > 1 || width != f.width else { return }
        f.origin.x -= width - f.width
        f.origin.y += f.height - size.height
        f.size = NSSize(width: width, height: size.height)
        window.setFrame(f, display: false)
        keepOnScreen()
    }

    func keepOnScreen() {
        guard let screen = (window.screen ?? NSScreen.main)?.visibleFrame else { return }
        var f = window.frame
        f.origin.x = min(max(f.origin.x, screen.minX), screen.maxX - f.width)
        f.origin.y = min(max(f.origin.y, screen.minY), screen.maxY - f.height)
        if f != window.frame { window.setFrame(f, display: false) }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
