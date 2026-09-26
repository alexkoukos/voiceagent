import Foundation

enum APIError: LocalizedError {
    case badStatus(Int, String)
    var errorDescription: String? {
        switch self {
        case .badStatus(401, _): return "Λάθος κλειδί. Έλεγξε τις Ρυθμίσεις (⚙︎)."
        case .badStatus(409, _): return "Η ηχογράφηση ανεβαίνει ακόμα. Δοκίμασε ξανά σε λίγα δευτερόλεπτα."
        case .badStatus(404, _): return "Δεν βρέθηκε. Μπορεί να έχει διαγραφεί."
        case .badStatus(422, _): return "Κάποιο πεδίο δεν είναι σωστό. Έλεγξε τι έγραψες."
        case .badStatus(let code, _): return "Κάτι πήγε στραβά στον server (\(code)). Δοκίμασε ξανά."
        }
    }
}

/// Turns any error into a short Greek message for the user.
func friendlyMessage(_ error: Error) -> String {
    if let e = error as? APIError { return e.errorDescription ?? "Κάτι πήγε στραβά." }
    if let e = error as? URLError {
        switch e.code {
        case .notConnectedToInternet, .networkConnectionLost: return "Δεν υπάρχει σύνδεση στο internet."
        case .badURL, .unsupportedURL, .cannotFindHost: return "Η διεύθυνση του server δεν είναι σωστή. Έλεγξε τις Ρυθμίσεις (⚙︎)."
        case .timedOut: return "Ο server αργεί να απαντήσει. Δοκίμασε ξανά."
        default: return "Δεν ήταν δυνατή η σύνδεση με τον server."
        }
    }
    return "Κάτι πήγε στραβά. Δοκίμασε ξανά."
}

enum Settings {
    // The API key lives in the Keychain; older builds kept it in UserDefaults, so move it over once.
    static var apiKey: String {
        get {
            if let legacy = UserDefaults.standard.string(forKey: "apiKey") {
                Keychain.set(legacy, for: "apiKey")
                UserDefaults.standard.removeObject(forKey: "apiKey")
            }
            return Keychain.get("apiKey") ?? ""
        }
        set { Keychain.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), for: "apiKey") }
    }
    static var baseURL: String {
        get { UserDefaults.standard.string(forKey: "baseURL") ?? "http://localhost:8000" }
        set {
            var url = newValue.trimmingCharacters(in: .whitespacesAndNewlines)
            while url.hasSuffix("/") { url.removeLast() }
            UserDefaults.standard.set(url, forKey: "baseURL")
        }
    }
}

struct APIClient {
    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        // Backend sends naive UTC timestamps, with or without fractional seconds.
        d.dateDecodingStrategy = .custom { decoder in
            let s = try decoder.singleValueContainer().decode(String.self)
            // Appointment times carry a timezone ("...Z" / "+03:00").
            let iso = ISO8601DateFormatter()
            for options: ISO8601DateFormatter.Options in [[.withInternetDateTime, .withFractionalSeconds], [.withInternetDateTime]] {
                iso.formatOptions = options
                if let date = iso.date(from: s) { return date }
            }
            if s.hasPrefix("20"), s.count == 10 {
                let f = DateFormatter()
                f.locale = Locale(identifier: "en_US_POSIX")
                f.dateFormat = "yyyy-MM-dd"
                if let date = f.date(from: s) { return date }
            }
            for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss"] {
                let f = DateFormatter()
                f.locale = Locale(identifier: "en_US_POSIX")
                f.timeZone = TimeZone(identifier: "UTC")
                f.dateFormat = format
                if let date = f.date(from: s) { return date }
            }
            throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "Bad date \(s)"))
        }
        return d
    }()

    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()

    private func request(_ method: String, _ path: String, body: (any Encodable)? = nil) async throws -> Data {
        guard let url = URL(string: Settings.baseURL + path) else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue(Settings.apiKey, forHTTPHeaderField: "x-api-key")
        if let body {
            req.httpBody = try Self.encoder.encode(body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.badStatus(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }
        return data
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try Self.decoder.decode(T.self, from: try await request("GET", path))
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: (any Encodable)? = nil) async throws -> T {
        try Self.decoder.decode(T.self, from: try await request(method, path, body: body))
    }

    func options() async throws -> ServerOptions { try await get("/options") }
    func friends() async throws -> [Friend] { try await get("/friends") }
    func addFriend(_ f: NewFriend) async throws -> Friend { try await send("POST", "/friends", body: f) }
    func updateFriend(_ id: String, _ f: NewFriend) async throws -> Friend { try await send("PUT", "/friends/\(id)", body: f) }
    func deleteFriend(_ id: String) async throws { _ = try await request("DELETE", "/friends/\(id)") }
    /// Includes deleted friends, so past calls keep their names.
    func allFriends() async throws -> [Friend] { try await get("/friends?include_deleted=true") }
    func templates() async throws -> [PromptTemplate] { try await get("/templates") }
    func addTemplate(_ t: NewTemplate) async throws -> PromptTemplate { try await send("POST", "/templates", body: t) }
    func deleteTemplate(_ id: String) async throws { _ = try await request("DELETE", "/templates/\(id)") }
    func calls() async throws -> [Call] { try await get("/calls") }
    func call(_ id: String) async throws -> Call { try await get("/calls/\(id)") }
    func startCall(_ c: NewCall) async throws -> Call { try await send("POST", "/calls", body: c) }
    func updates(_ id: String) throws -> URLSessionWebSocketTask {
        var wsBase = Settings.baseURL
        if wsBase.hasPrefix("https://") { wsBase = "wss://" + wsBase.dropFirst(8) }
        else if wsBase.hasPrefix("http://") { wsBase = "ws://" + wsBase.dropFirst(7) }
        guard let url = URL(string: wsBase + "/calls/\(id)/ws") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.setValue(Settings.apiKey, forHTTPHeaderField: "x-api-key")
        return URLSession.shared.webSocketTask(with: req)
    }
    func hangUp(_ id: String) async throws -> Call { try await send("POST", "/calls/\(id)/hangup") }
    func recordingURL(_ id: String) async throws -> URL {
        let link: RecordingLink = try await get("/calls/\(id)/recording")
        guard let url = URL(string: link.url) else { throw URLError(.badURL) }
        return url
    }
    func deleteRecording(_ id: String) async throws { _ = try await request("DELETE", "/calls/\(id)/recording") }

    // MARK: Receptionist (2.0)

    func practices() async throws -> [Practice] { try await get("/practices") }
    func practiceCalls(_ pid: String, outcome: String? = nil) async throws -> [ReceptionistCall] {
        try await get("/practices/\(pid)/calls" + (outcome.map { "?outcome=\($0)" } ?? ""))
    }
    func practiceCall(_ pid: String, _ id: String) async throws -> ReceptionistCall { try await get("/practices/\(pid)/calls/\(id)") }
    func review(_ pid: String, _ id: String, _ r: CallReview) async throws -> ReceptionistCall {
        try await send("PUT", "/practices/\(pid)/calls/\(id)/review", body: r)
    }
    func messages(_ pid: String) async throws -> [PracticeMessage] { try await get("/practices/\(pid)/messages") }
    func setMessage(_ pid: String, _ id: String, done: Bool) async throws -> PracticeMessage {
        try await send("PATCH", "/practices/\(pid)/messages/\(id)", body: ["status": done ? "done" : "new"])
    }
    func appointments(_ pid: String) async throws -> [Appointment] { try await get("/practices/\(pid)/appointments?upcoming=true") }
    func cancelAppointment(_ pid: String, _ id: String) async throws -> Appointment {
        try await send("DELETE", "/practices/\(pid)/appointments/\(id)")
    }
    func metrics(_ pid: String) async throws -> PracticeMetrics { try await get("/practices/\(pid)/metrics") }
    func handoffs(_ pid: String) async throws -> [Handoff] { try await get("/practices/\(pid)/handoffs") }
    func joinHandoff(_ pid: String, _ id: String, listenOnly: Bool) async throws -> RoomAccess {
        try await send("POST", "/practices/\(pid)/handoffs/\(id)/join", body: ["listen_only": listenOnly])
    }
    func registerDevice(token: String, practiceId: String?, sandbox: Bool) async throws {
        _ = try await request("POST", "/devices", body: DeviceRegistration(
            token: token, practiceId: practiceId, environment: sandbox ? "sandbox" : "production"))
    }
    func staff(_ pid: String) async throws -> [StaffMember] { try await get("/practices/\(pid)/staff") }
    func versions(_ pid: String) async throws -> [ConfigVersion] { try await get("/practices/\(pid)/versions") }
    func decide(_ pid: String, _ id: String, approve: Bool) async throws {
        _ = try await request("POST", "/practices/\(pid)/versions/\(id)/" + (approve ? "approve" : "reject"))
    }
    func rollback(_ pid: String, _ id: String) async throws {
        _ = try await request("POST", "/practices/\(pid)/versions/\(id)/rollback")
    }
    func closures(_ pid: String) async throws -> [Closure] { try await get("/practices/\(pid)/closures") }
    func addClosure(_ pid: String, _ c: NewClosure) async throws -> Closure {
        try await send("POST", "/practices/\(pid)/closures", body: c)
    }
    func deleteClosure(_ pid: String, _ id: String) async throws { _ = try await request("DELETE", "/practices/\(pid)/closures/\(id)") }
    func createLink(_ pid: String, staffId: String?) async throws -> AdminLink {
        try await send("POST", "/practices/\(pid)/links", body: ["staff_id": staffId])
    }
    func revokeLinks(_ pid: String) async throws { _ = try await request("DELETE", "/practices/\(pid)/links") }
    // Google Calendar (O3)
    func calendarConnections(_ pid: String) async throws -> [CalendarConnectionInfo] {
        try await get("/practices/\(pid)/calendar/connections")
    }
    func startCalendarConnect(_ pid: String, staffId: String?) async throws -> CalendarConnectStart {
        try await send("POST", "/practices/\(pid)/calendar/connect", body: ["staff_id": staffId])
    }
    func disconnectCalendar(_ pid: String, _ id: String) async throws {
        _ = try await request("DELETE", "/practices/\(pid)/calendar/connections/\(id)")
    }

    // Operations
    func alerts() async throws -> [OpsAlert] { try await get("/alerts") }
    func ackAlert(_ id: String) async throws { _ = try await request("POST", "/alerts/\(id)/ack") }
    func importGoogle(_ pid: String, query: String) async throws -> ConfigVersion? {
        try await send("POST", "/practices/\(pid)/imports/google", body: ["query": query])
    }
    func importPriceList(_ pid: String, _ upload: PriceListUpload) async throws -> ConfigVersion? {
        try await send("POST", "/practices/\(pid)/imports/price-list", body: upload)
    }
    func forwarding(_ pid: String, full: Bool) async throws -> ForwardingInfo {
        try await get("/practices/\(pid)/forwarding?mode=" + (full ? "full" : "backup"))
    }
    /// The raw JSON, to hand to the patient as a file.
    func exportCaller(_ pid: String, phone: String) async throws -> Data {
        try await request("POST", "/practices/\(pid)/data/export", body: ["phone": phone])
    }
    func eraseCaller(_ pid: String, phone: String) async throws -> [String: Int] {
        try await send("POST", "/practices/\(pid)/data/erase", body: ["phone": phone])
    }
    func usage(_ pid: String) async throws -> PracticeUsage { try await get("/practices/\(pid)/usage") }
    func setCostCap(_ pid: String, _ cap: Double?) async throws -> PracticeUsage {
        try await send("PUT", "/practices/\(pid)/cost-cap", body: ["monthly_cost_cap_eur": cap])
    }
    func block(_ pid: String, phone: String) async throws -> PracticeUsage {
        try await send("POST", "/practices/\(pid)/blocked", body: ["phone": phone])
    }
    func unblock(_ pid: String, phone: String) async throws -> PracticeUsage {
        try await send("POST", "/practices/\(pid)/unblock", body: ["phone": phone])
    }
    func setAdminPin(_ pid: String, pin: String?) async throws {
        _ = try await request("PUT", "/practices/\(pid)/admin-pin", body: ["pin": pin])
    }
    func exportCSV(_ pid: String) async throws -> Data { try await request("GET", "/practices/\(pid)/export.csv") }
    func offboard(_ pid: String) async throws { _ = try await request("POST", "/practices/\(pid)/offboard") }
    func reactivate(_ pid: String) async throws { _ = try await request("POST", "/practices/\(pid)/reactivate") }

    /// "changed" whenever a call, message or handoff of the practice changes.
    func practiceUpdates(_ pid: String) throws -> URLSessionWebSocketTask {
        try socket("/practices/\(pid)/ws")
    }

    private func socket(_ path: String) throws -> URLSessionWebSocketTask {
        var wsBase = Settings.baseURL
        if wsBase.hasPrefix("https://") { wsBase = "wss://" + wsBase.dropFirst(8) }
        else if wsBase.hasPrefix("http://") { wsBase = "ws://" + wsBase.dropFirst(7) }
        guard let url = URL(string: wsBase + path) else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.setValue(Settings.apiKey, forHTTPHeaderField: "x-api-key")
        return URLSession.shared.webSocketTask(with: req)
    }
}
