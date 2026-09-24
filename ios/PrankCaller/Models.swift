import Foundation

struct Friend: Codable, Identifiable, Hashable {
    let id: String
    let name: String
    let phoneNumber: String
}

struct PromptTemplate: Codable, Identifiable, Hashable {
    let id: String
    let title: String
    let persona: String
    let scenario: String
    let context: String
    let reveal: String
}

struct TranscriptEntry: Codable, Identifiable {
    let role: String
    let text: String
    let createdAt: Date
    var id: String { "\(createdAt.timeIntervalSince1970)-\(role)-\(text.hashValue)" }
}

struct Call: Codable, Identifiable {
    let id: String
    let friendId: String
    let status: String
    let persona: String
    let scenario: String
    let voice: String
    let recordingUrl: String?
    let durationSeconds: Int?
    let createdAt: Date
    let startedAt: Date?
    let endedAt: Date?
    var transcriptEntries: [TranscriptEntry]?

    var isInProgress: Bool { status == "pending" || status == "dialing" || status == "active" }
}

struct NewCall: Encodable {
    let friendId: String
    let persona: String
    let scenario: String
    let context: String
    let reveal: String
    let voice: String
    let maxDurationSeconds: Int
}

struct NewFriend: Encodable {
    let name: String
    let phoneNumber: String
}

struct NewTemplate: Encodable {
    let title: String
    let persona: String
    let scenario: String
    let context: String
    let reveal: String
}

struct RecordingLink: Decodable {
    let url: String
}
