// claudemon — a tiny floating terminal-style monitor for Claude Code token and agent usage.
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
    // subagents only
    let isAgent: Bool
    var agentType = "agent"
    var agentTask = ""

    init(isAgent: Bool) { self.isAgent = isAgent }
}

struct Agent {
    let type: String
    let task: String
    let tokens: Int
    let started: Date
    let running: Bool
}

struct Snapshot {
    var liveSessions = 0, busySessions = 0
    var context = 0, contextProject = ""
    var today = Usage()
    var window = 0
    var windowReset: Date?
    var rate: [Int] = []
    var perMinute = 0
    var models: [(String, Int)] = []
    var agents: [Agent] = []
    var agentsToday = 0
    var projects: [(String, Int)] = []
}

final class Store {
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
        if now.timeIntervalSince(lastDiscovery) > 3 {
            discover(since: now.addingTimeInterval(-86400))
            lastDiscovery = now
        }
        for (path, t) in transcripts {
            let mtime = (try? FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as? Date) ?? nil
            guard let mtime, mtime != t.mtime else { continue }
            t.mtime = mtime
            read(path, t)
        }
        let cutoff = now.addingTimeInterval(-86400)
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
            if isAgent,
               let data = try? Data(contentsOf: url.deletingPathExtension().appendingPathExtension("meta.json")),
               let meta = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                t.agentType = meta["agentType"] as? String ?? "agent"
                t.agentTask = meta["description"] as? String ?? ""
            }
            transcripts[path] = t
        }
    }

    private func read(_ path: String, _ t: Transcript) {
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
        let cutoff = Date().addingTimeInterval(-86400)
        for line in data.split(separator: 0x0A) {
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            if t.project == "?", let cwd = obj["cwd"] as? String { t.project = (cwd as NSString).lastPathComponent } // where the session started
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

    private func liveSessionCount() -> Int {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: sessionsDir.path)) ?? []
        return names.filter { $0.hasSuffix(".json") }.compactMap { pid_t($0.dropLast(5)) }
            .filter { kill($0, 0) == 0 || errno == EPERM }.count
    }

    func snapshot() -> Snapshot {
        let now = Date()
        var s = Snapshot()
        s.liveSessions = liveSessionCount()

        let mains = transcripts.filter { !$0.value.isAgent }
        s.busySessions = min(s.liveSessions, mains.values.filter { now.timeIntervalSince($0.mtime) < 20 }.count)
        if let current = mains.values.filter({ $0.lastReply != nil }).max(by: { $0.mtime < $1.mtime }) {
            s.context = current.lastContext
            s.contextProject = current.project
        }

        let startOfDay = Calendar.current.startOfDay(for: now)
        let all = replies.values.sorted { $0.time < $1.time }
        let today = all.filter { $0.time >= startOfDay }
        var byModel: [String: Int] = [:], byProject: [String: Int] = [:], byFile: [String: Int] = [:]
        for r in today {
            s.today += r.usage
            byModel[family(r.model), default: 0] += r.usage.total
            byProject[transcripts[r.file]?.project ?? "?", default: 0] += r.usage.total
        }
        for r in all { byFile[r.file, default: 0] += r.usage.total }
        s.models = byModel.sorted { $0.value > $1.value }
        s.projects = byProject.sorted { $0.value > $1.value }

        // Estimated 5-hour window: starts at the first reply after a gap of 5h or more.
        var windowStart: Date?
        for r in all where windowStart == nil || r.time >= windowStart!.addingTimeInterval(5 * 3600) {
            windowStart = r.time
        }
        if let ws = windowStart, now < ws.addingTimeInterval(5 * 3600) {
            s.windowReset = ws.addingTimeInterval(5 * 3600)
            s.window = all.filter { $0.time >= ws }.reduce(0) { $0 + $1.usage.total }
        }

        // Tokens per minute over the last hour, oldest first.
        var buckets = Array(repeating: 0, count: 60)
        for r in all where now.timeIntervalSince(r.time) < 3600 {
            let i = 59 - min(59, Int(now.timeIntervalSince(r.time) / 60))
            buckets[i] += r.usage.total
            if now.timeIntervalSince(r.time) < 300 { s.perMinute += r.usage.total }
        }
        s.perMinute /= 5
        s.rate = buckets

        let agents = transcripts.filter { $0.value.isAgent && ($0.value.lastReply ?? .distantPast) >= startOfDay }
        s.agentsToday = agents.count
        s.agents = agents.map { path, t in
            let running = now.timeIntervalSince(t.mtime) < 90 && t.lastStop != "end_turn"
            let started = replies.values.filter { $0.file == path }.map(\.time).min() ?? t.mtime
            return Agent(type: t.agentType, task: t.agentTask, tokens: byFile[path] ?? 0, started: started, running: running)
        }
        .sorted { ($0.running ? 1 : 0, $0.started) > ($1.running ? 1 : 0, $1.started) }
        return s
    }

    private func family(_ model: String) -> String {
        for f in ["opus", "sonnet", "haiku", "fable"] where model.contains(f) { return f }
        return "other"
    }
}

// MARK: - Rendering

enum Theme {
    static let bg = NSColor(srgbRed: 0.035, green: 0.045, blue: 0.06, alpha: 0.985)
    static let border = NSColor(srgbRed: 0.85, green: 0.47, blue: 0.34, alpha: 0.55)
    static let fg = NSColor(srgbRed: 0.55, green: 1.00, blue: 0.62, alpha: 1)
    static let dim = NSColor(srgbRed: 0.33, green: 0.40, blue: 0.45, alpha: 1)
    static let label = NSColor(srgbRed: 0.93, green: 0.56, blue: 0.40, alpha: 1)
    static let cyan = NSColor(srgbRed: 0.35, green: 0.85, blue: 0.95, alpha: 1)
    static let purple = NSColor(srgbRed: 0.72, green: 0.58, blue: 1.00, alpha: 1)
    static let warn = NSColor(srgbRed: 1.00, green: 0.78, blue: 0.25, alpha: 1)
    static let hot = NSColor(srgbRed: 1.00, green: 0.33, blue: 0.33, alpha: 1)
    static let white = NSColor(srgbRed: 0.86, green: 0.90, blue: 0.93, alpha: 1)
    static let font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
    static let bold = NSFont.monospacedSystemFont(ofSize: 11, weight: .bold)

    static func model(_ family: String) -> NSColor {
        switch family {
        case "opus": return purple
        case "sonnet": return fg
        case "haiku": return cyan
        case "fable": return warn
        default: return dim
        }
    }
}

final class Line {
    let s = NSMutableAttributedString()

    @discardableResult
    func add(_ text: String, _ color: NSColor = Theme.white, bold: Bool = false) -> Line {
        s.append(NSAttributedString(string: text, attributes: [.foregroundColor: color, .font: bold ? Theme.bold : Theme.font]))
        return self
    }

    @discardableResult
    func label(_ text: String) -> Line { add(text.padding(toLength: 10, withPad: " ", startingAt: 0), Theme.label, bold: true) }
}

let sparks = Array("▁▂▃▄▅▆▇█")

func tokens(_ n: Int) -> String {
    let d = Double(n)
    if n >= 1_000_000 { return String(format: "%.1fM", d / 1_000_000) }
    if n >= 10_000 { return String(format: "%.0fk", d / 1000) }
    if n >= 1000 { return String(format: "%.1fk", d / 1000) }
    return "\(n)"
}

func clip(_ s: String, _ n: Int) -> String {
    s.count <= n ? s.padding(toLength: n, withPad: " ", startingAt: 0) : String(s.prefix(n - 1)) + "…"
}

func elapsed(_ from: Date) -> String {
    let s = Int(Date().timeIntervalSince(from))
    return s >= 3600 ? "\(s / 3600)h\(s % 3600 / 60)m" : s >= 60 ? "\(s / 60)m" : "\(s)s"
}

final class MonitorView: NSView {
    var snap = Snapshot()
    var spinPhase = true
    let clock: DateFormatter = { let f = DateFormatter(); f.dateFormat = "HH:mm:ss"; return f }()
    let hm: DateFormatter = { let f = DateFormatter(); f.dateFormat = "HH:mm"; return f }()
    let pad = NSSize(width: 14, height: 10)
    let titleHeight: CGFloat = 24

    override var isFlipped: Bool { true }

    func body() -> NSAttributedString {
        let s = snap
        var lines: [Line] = []

        let ses = Line().label("Sessions").add("\(s.liveSessions)", Theme.fg, bold: true).add(" live", Theme.dim)
        if s.busySessions > 0 { ses.add(" · ", Theme.dim).add("\(s.busySessions) busy", Theme.warn) }
        if s.context > 0 {
            ses.add(" · ctx ", Theme.dim).add(tokens(s.context)).add(" \(clip(s.contextProject, 16).trimmingCharacters(in: .whitespaces))", Theme.dim)
        }
        lines.append(ses)

        let tok = Line().label("Tokens").add("today ", Theme.dim).add(tokens(s.today.total), Theme.fg, bold: true)
        if let reset = s.windowReset {
            tok.add("  5h ", Theme.dim).add(tokens(s.window), Theme.fg, bold: true).add(" · resets ~\(hm.string(from: reset))", Theme.dim)
        }
        lines.append(tok)
        lines.append(Line().add("          ").add("in ", Theme.dim).add(tokens(s.today.input + s.today.cacheWrite))
            .add("  out ", Theme.dim).add(tokens(s.today.output))
            .add("  cache ", Theme.dim).add(tokens(s.today.cacheRead)))

        lines.append(Line().label("Speed").add(tokens(s.perMinute), s.perMinute > 0 ? Theme.fg : Theme.dim, bold: true)
            .add("/min now", Theme.dim).add(" · peak ", Theme.dim).add(tokens(s.rate.max() ?? 0)).add("/min", Theme.dim))

        let total = max(1, s.models.reduce(0) { $0 + $1.1 })
        let mdl = Line().label("Models")
        if s.models.isEmpty {
            mdl.add("no replies today", Theme.dim)
        } else {
            var used = 0
            for (i, (fam, n)) in s.models.enumerated() {
                let w = i == s.models.count - 1 ? 18 - used : max(1, n * 18 / total)
                mdl.add(String(repeating: "█", count: max(0, w)), Theme.model(fam))
                used += w
            }
            for (fam, n) in s.models.prefix(3) { mdl.add(" \(fam) ", Theme.model(fam)).add(n * 100 < total ? "<1%" : "\(n * 100 / total)%", Theme.dim) }
        }
        lines.append(mdl)

        let running = s.agents.filter(\.running).count
        lines.append(Line().label("Agents").add("\(running)", running > 0 ? Theme.warn : Theme.dim, bold: true)
            .add(" running", Theme.dim).add(" · ", Theme.dim).add("\(s.agentsToday)").add(" today", Theme.dim))
        for a in s.agents.prefix(4) {
            let icon = a.running ? (spinPhase ? "◐ " : "◓ ") : "✓ "
            lines.append(Line().add("  ").add(icon, a.running ? Theme.warn : Theme.fg)
                .add(clip(a.type, 15), a.running ? Theme.white : Theme.dim)
                .add(" " + clip(a.task, 22), Theme.dim)
                .add(" " + tokens(a.tokens).padding(toLength: 6, withPad: " ", startingAt: 0), a.running ? Theme.white : Theme.dim)
                .add(a.running ? elapsed(a.started) : "", Theme.warn))
        }

        if !s.projects.isEmpty {
            let top = Line().label("Projects")
            for (i, (name, n)) in s.projects.prefix(2).enumerated() {
                if i > 0 { top.add(" · ", Theme.dim) }
                top.add(clip(name, 16).trimmingCharacters(in: .whitespaces)).add(" \(tokens(n))", Theme.dim)
            }
            lines.append(top)
        }


        let out = NSMutableAttributedString()
        for (i, l) in lines.enumerated() {
            if i > 0 { out.append(NSAttributedString(string: "\n")) }
            out.append(l.s)
        }
        let para = NSMutableParagraphStyle()
        para.lineSpacing = 2
        out.addAttribute(.paragraphStyle, value: para, range: NSRange(location: 0, length: out.length))
        return out
    }

    // Line chart under the text: header, plot, axis labels.
    let chartGap: CGFloat = 12, chartHeader: CGFloat = 16, chartPlot: CGFloat = 56, chartAxis: CGFloat = 14
    var chartBlock: CGFloat { chartGap + chartHeader + chartPlot + chartAxis }
    var chartRect = NSRect.zero
    var hoverIndex: Int?
    let small = NSFont.monospacedSystemFont(ofSize: 9.5, weight: .regular)

    func fittingSize() -> NSSize {
        let b = body().size()
        return NSSize(width: ceil(b.width) + pad.width * 2 + 8,
                      height: ceil(b.height) + titleHeight + pad.height * 2 + chartBlock)
    }

    func text(_ s: String, _ color: NSColor, _ font: NSFont? = nil) -> NSAttributedString {
        NSAttributedString(string: s, attributes: [.font: font ?? small, .foregroundColor: color])
    }

    func drawChart(top: CGFloat) {
        let data = snap.rate
        let left = pad.width, width = bounds.width - pad.width * 2
        text("Tokens per minute", Theme.white).draw(at: NSPoint(x: left, y: top))
        let span = NSMutableAttributedString(attributedString: text("max ", Theme.dim))
        span.append(text(tokens(data.max() ?? 0), Theme.white))
        span.draw(at: NSPoint(x: left + width - span.size().width, y: top))

        let r = NSRect(x: left, y: top + chartHeader, width: width, height: chartPlot)
        chartRect = r
        guard data.count > 1 else { return }
        let peak = CGFloat(max(1, data.max() ?? 1))
        func point(_ i: Int) -> NSPoint {
            NSPoint(x: r.minX + r.width * CGFloat(i) / CGFloat(data.count - 1),
                    y: r.maxY - (r.height - 4) * CGFloat(data[i]) / peak)
        }

        // Recessive grid: baseline plus half and full scale.
        for f in [0.0, 0.5, 1.0] as [CGFloat] {
            let y = (r.minY + 4 + (r.height - 4) * f).rounded() + 0.5
            let g = NSBezierPath()
            g.move(to: NSPoint(x: r.minX, y: y)); g.line(to: NSPoint(x: r.maxX, y: y))
            g.lineWidth = 1
            if f < 1 { g.setLineDash([2, 3], count: 2, phase: 0) }
            Theme.dim.withAlphaComponent(f < 1 ? 0.25 : 0.45).setStroke()
            g.stroke()
        }

        let line = NSBezierPath()
        line.move(to: point(0))
        for i in 1..<data.count { line.line(to: point(i)) }
        let area = line.copy() as! NSBezierPath
        area.line(to: NSPoint(x: r.maxX, y: r.maxY))
        area.line(to: NSPoint(x: r.minX, y: r.maxY))
        area.close()
        NSGradient(starting: Theme.fg.withAlphaComponent(0.30), ending: Theme.fg.withAlphaComponent(0.0))?
            .draw(in: area, angle: 90)
        line.lineWidth = 2
        line.lineJoinStyle = .round
        line.lineCapStyle = .round
        Theme.fg.setStroke()
        line.stroke()

        func dot(_ p: NSPoint) {
            Theme.bg.setFill()
            NSBezierPath(ovalIn: NSRect(x: p.x - 6, y: p.y - 6, width: 12, height: 12)).fill()
            Theme.fg.setFill()
            NSBezierPath(ovalIn: NSRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8)).fill()
        }

        let axisY = r.maxY + 3
        text("60m", Theme.dim).draw(at: NSPoint(x: r.minX, y: axisY))
        let mid = text("30m", Theme.dim)
        mid.draw(at: NSPoint(x: r.midX - mid.size().width / 2, y: axisY))
        let now = text("now", Theme.dim)
        now.draw(at: NSPoint(x: r.maxX - now.size().width, y: axisY))

        guard let i = hoverIndex, data.indices.contains(i) else { dot(point(data.count - 1)); return }
        let p = point(i)
        let cross = NSBezierPath()
        cross.move(to: NSPoint(x: p.x.rounded() + 0.5, y: r.minY)); cross.line(to: NSPoint(x: p.x.rounded() + 0.5, y: r.maxY))
        cross.lineWidth = 1
        Theme.white.withAlphaComponent(0.35).setStroke()
        cross.stroke()
        dot(p)

        let when = hm.string(from: Date().addingTimeInterval(-Double(data.count - 1 - i) * 60))
        let tip = NSMutableAttributedString(attributedString: text(when + "  ", Theme.dim))
        tip.append(text(tokens(data[i]) + " tokens", Theme.white))
        let ts = tip.size()
        var box = NSRect(x: p.x + 8, y: r.minY, width: ts.width + 12, height: ts.height + 6)
        if box.maxX > bounds.width - 4 { box.origin.x = p.x - 8 - box.width }
        let bubble = NSBezierPath(roundedRect: box, xRadius: 4, yRadius: 4)
        NSColor(srgbRed: 0.10, green: 0.12, blue: 0.15, alpha: 0.95).setFill()
        bubble.fill()
        Theme.border.withAlphaComponent(0.5).setStroke()
        bubble.lineWidth = 1
        bubble.stroke()
        tip.draw(at: NSPoint(x: box.minX + 6, y: box.minY + 3))
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }

    override func mouseMoved(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        let n = snap.rate.count
        var i: Int?
        if n > 1, chartRect.insetBy(dx: -6, dy: -8).contains(p) {
            i = max(0, min(n - 1, Int(((p.x - chartRect.minX) / chartRect.width * CGFloat(n - 1)).rounded())))
        }
        if i != hoverIndex { hoverIndex = i; needsDisplay = true }
    }

    override func mouseExited(with event: NSEvent) {
        if hoverIndex != nil { hoverIndex = nil; needsDisplay = true }
    }

    override func draw(_ dirtyRect: NSRect) {
        let shape = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 9, yRadius: 9)
        Theme.bg.setFill()
        shape.fill()

        NSGraphicsContext.saveGraphicsState()
        shape.addClip()
        NSColor(srgbRed: 0.85, green: 0.47, blue: 0.34, alpha: 0.07).setFill()
        NSRect(x: 0, y: 0, width: bounds.width, height: titleHeight).fill()
        NSGraphicsContext.restoreGraphicsState()

        Theme.border.setStroke()
        shape.lineWidth = 1
        shape.stroke()
        Theme.border.withAlphaComponent(0.3).setFill()
        NSRect(x: 0, y: titleHeight, width: bounds.width, height: 1).fill()

        let title = Line().add("● ", Theme.hot).add("● ", Theme.warn).add("●", Theme.fg).add("   claudemon", Theme.dim).s
        title.draw(at: NSPoint(x: 10, y: (titleHeight - title.size().height) / 2))
        let clk = NSAttributedString(string: clock.string(from: Date()), attributes: [.font: Theme.font, .foregroundColor: Theme.dim])
        clk.draw(at: NSPoint(x: bounds.width - clk.size().width - 12, y: (titleHeight - clk.size().height) / 2))

        let b = body()
        b.draw(at: NSPoint(x: pad.width, y: titleHeight + pad.height))
        drawChart(top: titleHeight + pad.height + ceil(b.size().height) + chartGap)
    }

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        if p.y < titleHeight && p.x < 22 { NSApp.terminate(nil); return }
        window?.performDrag(with: event)
    }

    override func rightMouseDown(with event: NSEvent) {
        let menu = NSMenu()
        let pin = NSMenuItem(title: "Always on Top", action: #selector(AppDelegate.togglePin), keyEquivalent: "")
        pin.state = window?.level == .floating ? .on : .off
        menu.addItem(pin)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit claudemon", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }

    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func keyDown(with event: NSEvent) {
        if event.charactersIgnoringModifiers == "q" || event.keyCode == 53 { NSApp.terminate(nil) }
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
    var ticks = 0

    func applicationDidFinishLaunching(_ note: Notification) {
        queue.sync { store.refresh(); view.snap = store.snapshot() }
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
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.tick() }
    }

    func tick() {
        ticks += 1
        view.spinPhase.toggle()
        if ticks % 2 == 0 {
            queue.async { [weak self] in
                guard let self else { return }
                self.store.refresh()
                let snap = self.store.snapshot()
                DispatchQueue.main.async { self.view.snap = snap; self.resize() }
            }
        }
        view.needsDisplay = true
    }

    func resize() {
        let size = view.fittingSize()
        guard abs(size.height - window.frame.height) > 1 || size.width > window.frame.width else { return }
        var f = window.frame
        let width = max(size.width, f.width)
        f.origin.x -= width - f.width          // grow leftwards: it sits at the right edge of the screen
        f.origin.y += f.height - size.height   // and keep the title bar where it was
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

    @objc func togglePin() {
        window.level = window.level == .floating ? .normal : .floating
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
