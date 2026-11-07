import SwiftUI

struct TranscriptEntry: Codable, Identifiable {
    let role: String
    let text: String
    let createdAt: Date
    var id: String { "\(createdAt.timeIntervalSince1970)-\(role)-\(text.hashValue)" }
}

struct ServerOptions: Decodable {
    let accountScope: String?
}

struct RecordingLink: Decodable {
    let url: String
}
