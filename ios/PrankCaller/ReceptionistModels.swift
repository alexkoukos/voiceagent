import Foundation

struct Practice: Codable, Identifiable, Hashable {
    let id: String
    let name: String
    let slug: String?
    let language: String
}

struct RoutingDecision: Codable, Identifiable {
    let kind: String
    let value: String
    let rule: String
    let path: String
    let createdAt: Date
    var id: String { "\(createdAt.timeIntervalSince1970)-\(kind)-\(rule)" }
}

struct PracticeMessage: Codable, Identifiable {
    let id: String
    let callId: String?
    let callerName: String
    let callbackNumber: String?
    let reason: String
    let bestTime: String
    let urgent: Bool
    let status: String
    let createdAt: Date
}

struct Handoff: Codable, Identifiable {
    let id: String
    let callId: String
    let staffId: String?
    let mode: String
    let status: String
    let createdAt: Date
}

struct Appointment: Codable, Identifiable {
    let id: String
    let callId: String?
    let customerName: String
    let customerPhone: String?
    let serviceId: String
    let serviceName: String
    let startsAt: Date
    let endsAt: Date
    let status: String
}

struct ReceptionistCall: Codable, Identifiable {
    let id: String
    let direction: String
    let callerNumber: String?
    let status: String
    let useCase: String?
    let outcome: String?
    let summary: String?
    let flags: [String]
    let costEstimate: Double?
    let latencyMsMedian: Int?
    let hoursState: String?
    let purpose: String?
    let review: [String: ReviewValue]?
    let recordingUrl: String?
    let durationSeconds: Int?
    let createdAt: Date
    var transcriptEntries: [TranscriptEntry]?
    var routing: [RoutingDecision]?
    var messages: [PracticeMessage]?
    var handoffs: [Handoff]?
    var appointment: Appointment?

    var isInProgress: Bool { ["pending", "queued", "dialing", "active"].contains(status) }
    var caller: String {
        if direction == "web" { return "Web demo" }
        return callerNumber ?? "Άγνωστος αριθμός"
    }
    // The decoder's snake_case conversion also renames dictionary keys, so accept both spellings.
    var routingCorrect: Bool? { (review?["routingCorrect"] ?? review?["routing_correct"])?.bool }
    var bookingCorrect: Bool? { (review?["bookingCorrect"] ?? review?["booking_correct"])?.bool }

    var outcomeText: String {
        if isInProgress { return "Σε εξέλιξη" }
        return Self.label(outcome ?? "")
    }

    /// Outcome keys arrive as "message_taken" or, inside dictionaries, "messageTaken".
    static func label(_ key: String) -> String {
        if let l = outcomeLabels[key] { return l }
        let snake = key.reduce(into: "") { $0 += $1.isUppercase ? "_" + $1.lowercased() : String($1) }
        return outcomeLabels[snake] ?? key
    }

    static let outcomeLabels: [String: String] = [
        "booked": "Νέο ραντεβού", "rescheduled": "Αλλαγή ραντεβού", "cancelled": "Ακύρωση",
        "confirmed": "Επιβεβαίωση", "info_given": "Πληροφορίες", "message_taken": "Μήνυμα",
        "transferred": "Σε άνθρωπο", "abandoned": "Έκλεισε", "failed": "Αποτυχία",
    ]

    var outcomeIcon: String {
        if isInProgress { return "waveform" }
        switch outcome {
        case "booked", "rescheduled", "confirmed": return "calendar.badge.checkmark"
        case "cancelled": return "calendar.badge.minus"
        case "message_taken": return "envelope.fill"
        case "transferred": return "person.wave.2.fill"
        case "info_given": return "info.circle.fill"
        case "abandoned": return "phone.down"
        default: return "exclamationmark.triangle.fill"
        }
    }

    static let flagLabels: [String: String] = [
        "urgent": "Επείγον", "emergency": "Επείγον περιστατικό", "name_uncertain": "Αβέβαιο όνομα",
        "tool_error": "Σφάλμα συστήματος", "over_duration": "Ξεπέρασε τον χρόνο", "recording_refused": "Χωρίς ηχογράφηση",
    ]
}

/// Review values are JSON bools or strings; decode either.
enum ReviewValue: Codable {
    case bool(Bool), text(String), none
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let t = try? c.decode(String.self) { self = .text(t) }
        else { self = .none }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .bool(let b): try c.encode(b)
        case .text(let t): try c.encode(t)
        case .none: try c.encodeNil()
        }
    }
    var bool: Bool? { if case .bool(let b) = self { return b } else { return nil } }
}

struct CallReview: Encodable {
    let routingCorrect: Bool?
    let bookingCorrect: Bool?
    var note: String = ""
}

struct PracticeMetrics: Decodable {
    let days: Int
    let calls: Int
    let answered: Int
    let outcomes: [String: Int]
    let routingAccuracyPct: Double?
    let routingReviewed: Int
    let bookingAccuracyPct: Double?
    let bookingReviewed: Int
    let resolvedWithoutHumanPct: Double?
    let handoffPct: Double?
    let abandonedPct: Double?
    let latencyMsMedian: Int?
    let notifiedWithin60sPct: Double?
    let costPerMinuteEur: Double?
    let costTotalEur: Double
    let bookings: Int
    let valueEstimateEur: Double
    let guaranteeThreshold: Int
}

struct RoomAccess: Decodable {
    let url: String
    let token: String
    let callId: String
}

struct DeviceRegistration: Encodable {
    let token: String
    let practiceId: String?
    let environment: String
}

// MARK: Changes after go-live (OP2, OP3)

struct StaffMember: Codable, Identifiable, Hashable {
    let id: String
    let name: String
    let role: String
}

struct Closure: Decodable, Identifiable {
    let id: String
    let dateFrom: Date
    let dateTo: Date
    let staffId: String?
    let reason: String?
    let toRebook: [Appointment]

    var range: String {
        let f = Date.FormatStyle.dateTime.weekday(.abbreviated).day().month(.wide)
        if Calendar.current.isDate(dateFrom, inSameDayAs: dateTo) { return dateFrom.formatted(f) }
        return "\(dateFrom.formatted(f)) – \(dateTo.formatted(f))"
    }

    /// "2026-08-10" in the phone's calendar, as the backend expects.
    static func day(_ date: Date) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }
}

struct NewClosure: Encodable {
    let dateFrom: String
    let dateTo: String
    let staffId: String?
    let reason: String?
}

struct AdminLink: Decodable {
    let url: String
    let expiresAt: Date
}

/// Any JSON value (the changed fields of a config version).
indirect enum JSONValue: Decodable, Hashable {
    case string(String), number(Double), bool(Bool), array([JSONValue]), object([String: JSONValue]), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }
    var text: String {
        switch self {
        case .string(let s): return s
        case .number(let n): return n == n.rounded() ? String(Int(n)) : String(n)
        case .bool(let b): return b ? "ναι" : "όχι"
        case .array(let a): return a.map(\.text).joined(separator: ", ")
        case .object(let o): return o.map { "\($0.key): \($0.value.text)" }.joined(separator: ", ")
        case .null: return "—"
        }
    }
    subscript(_ key: String) -> JSONValue? { if case .object(let o) = self { return o[key] } else { return nil } }
}

struct ConfigVersion: Decodable, Identifiable {
    let id: String
    let status: String
    let source: String
    let author: String
    let summary: String
    let changes: [String: JSONValue]
    let createdAt: Date
    let decidedAt: Date?

    var sourceLabel: String {
        switch source {
        case "link": return "Σύνδεσμος γιατρού"
        case "rollback": return "Επαναφορά"
        case "baseline": return "Αρχική κατάσταση"
        default: return "Εφαρμογή"
        }
    }

    /// What the approver sees: new services with prices, or new information entries.
    var previewLines: [String] {
        var lines: [String] = []
        if case .array(let services)? = changes["services"] {
            for s in services {
                let price = s["price"].map(\.text) ?? "—"
                let minutes = s["durationMinutes"]?.text ?? s["duration_minutes"]?.text ?? "?"
                lines.append("• \(s["name"]?.text ?? "?"): \(price), \(minutes)′")
            }
        }
        // The decoder camel-cases dictionary keys too.
        if case .object(let kb)? = changes["knowledgeBase"] ?? changes["knowledge_base"] {
            for (k, v) in kb.sorted(by: { $0.key < $1.key }) { lines.append("• \(k): \(v.text)") }
        }
        return lines
    }
}
