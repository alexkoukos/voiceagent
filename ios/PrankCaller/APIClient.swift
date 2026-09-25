import Foundation

enum APIError: LocalizedError {
    case badStatus(Int, String)
    var errorDescription: String? {
        switch self {
        case .badStatus(let code, let body): return "Server returned \(code): \(body)"
        }
    }
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
        set { Keychain.set(newValue, for: "apiKey") }
    }
    static var baseURL: String {
        get { UserDefaults.standard.string(forKey: "baseURL") ?? "http://localhost:8000" }
        set { UserDefaults.standard.set(newValue, forKey: "baseURL") }
    }
}

struct APIClient {
    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        // Backend sends naive UTC timestamps, with or without fractional seconds.
        d.dateDecodingStrategy = .custom { decoder in
            let s = try decoder.singleValueContainer().decode(String.self)
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
}
